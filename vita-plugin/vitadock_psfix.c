#include <psp2kern/kernel/modulemgr.h>
#include <psp2kern/kernel/threadmgr.h>
#include <psp2kern/io/fcntl.h>
#include <psp2kern/udcd.h>
#include <taihen.h>

int module_get_export_func(SceUID pid, const char *modname, uint32_t libnid,
                           uint32_t funcnid, uintptr_t *func);

#define VERSION "1.1"
#define LOG_PATH "ur0:data/vitadock_psfix.txt"

struct panel {
	const char *module;
	uint32_t lib, nid_on, nid_off, nid_get, nid_set;
};

static const struct panel PANELS[] = {
	{ "SceLcd",  0xFA916D71, 0x5F4124AB, 0x1A0A7519, 0x3A6D6AC3, 0x581D3A87 },
	{ "SceOled", 0x60C7478A, 0x4C7836C7, 0xBC84602E, 0x43EF811A, 0xF9624C47 },
};

static const char *VIDEO_PLUGINS[] = { "vitastream", "udcd_uvc" };

#define FALLBACK_BRIGHTNESS 0xA000

static const struct panel *panel;
static int (*panel_on)(void);
static int (*panel_off)(void);
static SceUInt32 (*panel_get)(void);
static int (*panel_set)(SceUInt32);

static int (*module_info_by_addr)(const void *addr, SceKernelModuleInfo *info);

static SceUID hook_on_id = -1, hook_off_id = -1, hook_set_id = -1;
static tai_hook_ref_t hook_on_ref, hook_off_ref, hook_set_ref;

static volatile int holding;
static volatile SceUInt32 restore;
static volatile int running;
static SceUID watcher = -1;

static char logbuf[1024];
static volatile unsigned loglen;
static volatile int log_dropped;

static void log_add(const char *s)
{
	while (*s) {
		if (loglen >= sizeof(logbuf)) {
			log_dropped = 1;
			return;
		}
		logbuf[loglen++] = *s++;
	}
}

static void log_hex(uint32_t v)
{
	char b[11] = "0x";
	for (int i = 0; i < 8; i++)
		b[2 + i] = "0123456789abcdef"[(v >> (28 - 4 * i)) & 15];
	b[10] = 0;
	log_add(b);
}

static void log_line(const char *what, uint32_t v)
{
	log_add(what);
	log_add(" ");
	log_hex(v);
	log_add("\n");
}

static void log_flush(int truncate)
{
	unsigned n = loglen;
	if (!n && !truncate)
		return;
	SceUID fd = ksceIoOpen(LOG_PATH, SCE_O_WRONLY | SCE_O_CREAT | (truncate ? SCE_O_TRUNC : SCE_O_APPEND), 0666);
	if (fd >= 0) {
		if (n)
			ksceIoWrite(fd, logbuf, n);
		if (log_dropped)
			ksceIoWrite(fd, "(some lines dropped)\n", 21);
		ksceIoClose(fd);
	}
	loglen = 0;
	log_dropped = 0;
}

static int same(const char *a, const char *b)
{
	while (*a && *a == *b) {
		a++;
		b++;
	}
	return *a == *b;
}

static int from_video_plugin(const void *ret_addr)
{
	SceKernelModuleInfo info;
	char *p = (char *)&info;
	for (unsigned i = 0; i < sizeof(info); i++)
		p[i] = 0;
	info.size = sizeof(info);
	if (!module_info_by_addr ||
	    module_info_by_addr((const void *)((uintptr_t)ret_addr & ~1u), &info) < 0)
		return 0;
	info.module_name[sizeof(info.module_name) - 1] = 0;
	for (unsigned i = 0; i < sizeof(VIDEO_PLUGINS) / sizeof(VIDEO_PLUGINS[0]); i++)
		if (same(info.module_name, VIDEO_PLUGINS[i]))
			return 1;
	return 0;
}

static int cable_out(void)
{
	int s = ksceUdcdGetDeviceState();
	return s >= 0 && (s & SCE_UDCD_STATUS_CABLE_DISCONNECTED);
}

static int hook_off(void)
{
	const void *caller = __builtin_return_address(0);
	SceUInt32 before = panel_get();
	int ret = TAI_CONTINUE(int, hook_off_ref);
	if (!holding && from_video_plugin(caller)) {
		if (before)
			restore = before;
		holding = 1;
		log_line("docked: holding the screen off, will restore brightness", restore);
	}
	return ret;
}

static int hook_on(void)
{
	const void *caller = __builtin_return_address(0);
	if (holding) {
		if (!from_video_plugin(caller)) {
			log_line("held back a screen-on from", (uint32_t)(uintptr_t)caller);
			return 0;
		}
		holding = 0;
		log_line("undocked: letting the screen back on", restore);
	}
	return TAI_CONTINUE(int, hook_on_ref);
}

static int hook_set(SceUInt32 brightness)
{
	const void *caller = __builtin_return_address(0);
	if (brightness && holding && !from_video_plugin(caller)) {
		restore = brightness;
		log_line("held back a brightness change to", brightness);
		return 0;
	}
	if (brightness && !holding)
		restore = brightness;
	return TAI_CONTINUE(int, hook_set_ref, brightness);
}

static void let_go(const char *why)
{
	holding = 0;
	log_line(why, restore);
	panel_on();
	panel_set(restore ? restore : FALLBACK_BRIGHTNESS);
}

static int watch(SceSize args, void *argp)
{
	(void)args;
	(void)argp;
	int ticks = 0;
	while (running) {
		if (holding) {
			if (cable_out()) {
				let_go("cable out: putting the screen back");
			} else if (panel_get() != 0) {
				log_line("screen came on by another route, switching it off", panel_get());
				panel_off();
			}
		}
		if (++ticks >= 3) {
			ticks = 0;
			log_flush(0);
		}
		ksceKernelDelayThread(1000 * 1000);
	}
	log_flush(0);
	return 0;
}

static void unhook(void)
{
	if (hook_set_id >= 0)
		taiHookReleaseForKernel(hook_set_id, hook_set_ref);
	if (hook_on_id >= 0)
		taiHookReleaseForKernel(hook_on_id, hook_on_ref);
	if (hook_off_id >= 0)
		taiHookReleaseForKernel(hook_off_id, hook_off_ref);
	hook_set_id = hook_on_id = hook_off_id = -1;
}

static int find_panel(void)
{
	for (unsigned i = 0; i < sizeof(PANELS) / sizeof(PANELS[0]); i++) {
		const struct panel *p = &PANELS[i];
		uintptr_t on, off, get, set;
		if (module_get_export_func(KERNEL_PID, p->module, p->lib, p->nid_on, &on) < 0 ||
		    module_get_export_func(KERNEL_PID, p->module, p->lib, p->nid_off, &off) < 0 ||
		    module_get_export_func(KERNEL_PID, p->module, p->lib, p->nid_get, &get) < 0 ||
		    module_get_export_func(KERNEL_PID, p->module, p->lib, p->nid_set, &set) < 0)
			continue;
		panel = p;
		panel_on = (int (*)(void))on;
		panel_off = (int (*)(void))off;
		panel_get = (SceUInt32 (*)(void))get;
		panel_set = (int (*)(SceUInt32))set;
		return 0;
	}
	return -1;
}

void _start() __attribute__ ((weak, alias("module_start")));

int module_start(SceSize args, void *argp)
{
	(void)args;
	(void)argp;

	log_add("vitadock_psfix " VERSION "\n");
	if (find_panel() < 0) {
		log_add("couldn't find the screen driver, not doing anything\n");
		log_flush(1);
		return SCE_KERNEL_START_FAILED;
	}
	uintptr_t f;
	if (module_get_export_func(KERNEL_PID, "SceKernelModulemgr", 0xD4A60A52, 0x1D9E0F7E, &f) < 0) {
		log_add("couldn't find ksceKernelGetModuleInfoByAddr, not doing anything\n");
		log_flush(1);
		return SCE_KERNEL_START_FAILED;
	}
	module_info_by_addr = (int (*)(const void *, SceKernelModuleInfo *))f;
	log_add("screen driver: ");
	log_add(panel->module);
	log_add("\n");

	hook_off_id = taiHookFunctionExportForKernel(KERNEL_PID, &hook_off_ref, panel->module,
	                                             panel->lib, panel->nid_off, hook_off);
	hook_on_id = taiHookFunctionExportForKernel(KERNEL_PID, &hook_on_ref, panel->module,
	                                            panel->lib, panel->nid_on, hook_on);
	hook_set_id = taiHookFunctionExportForKernel(KERNEL_PID, &hook_set_ref, panel->module,
	                                             panel->lib, panel->nid_set, hook_set);
	if (hook_off_id < 0 || hook_on_id < 0 || hook_set_id < 0) {
		log_line("couldn't hook the screen driver", (uint32_t)(hook_off_id < 0 ? hook_off_id :
		                                                       hook_on_id < 0 ? hook_on_id : hook_set_id));
		unhook();
		log_flush(1);
		return SCE_KERNEL_START_FAILED;
	}
	log_add("ready, waiting for the video plugin\n");
	log_flush(1);

	running = 1;
	watcher = ksceKernelCreateThread("vitadock_psfix", watch, 0x3C, 0x2000, 0, 0, NULL);
	if (watcher >= 0)
		ksceKernelStartThread(watcher, 0, NULL);

	return SCE_KERNEL_START_SUCCESS;
}

int module_stop(SceSize args, void *argp)
{
	(void)args;
	(void)argp;

	running = 0;
	if (watcher >= 0) {
		ksceKernelWaitThreadEnd(watcher, NULL, NULL);
		ksceKernelDeleteThread(watcher);
		watcher = -1;
	}
	unhook();
	if (holding)
		let_go("plugin stopping: putting the screen back");
	return SCE_KERNEL_STOP_SUCCESS;
}

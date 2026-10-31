#include <psp2kern/kernel/modulemgr.h>
#include <psp2kern/kernel/threadmgr.h>
#include <psp2kern/udcd.h>
#include <taihen.h>

int module_get_export_func(SceUID pid, const char *modname, uint32_t libnid,
                           uint32_t funcnid, uintptr_t *func);

struct panel {
	const char *module;
	uint32_t lib, nid_on, nid_off, nid_get, nid_set;
};

static const struct panel PANELS[] = {
	{ "SceLcd",  0xFA916D71, 0x5F4124AB, 0x1A0A7519, 0x3A6D6AC3, 0x581D3A87 },
	{ "SceOled", 0x60C7478A, 0x4C7836C7, 0xBC84602E, 0x43EF811A, 0xF9624C47 },
};

static const char *VIDEO_DRIVERS[] = { "vitastream", "VITAUVC00" };

#define FALLBACK_BRIGHTNESS 0xA000

static const struct panel *panel;
static int (*panel_on)(void);
static int (*panel_off)(void);
static SceUInt32 (*panel_get)(void);
static int (*panel_set)(SceUInt32);

static SceUID hook_on_id = -1, hook_off_id = -1, hook_set_id = -1;
static tai_hook_ref_t hook_on_ref, hook_off_ref, hook_set_ref;

static volatile int holding;
static volatile SceUInt32 restore;
static volatile int running;
static SceUID watcher = -1;

static int video_plugin_running(void)
{
	for (unsigned i = 0; i < sizeof(VIDEO_DRIVERS) / sizeof(VIDEO_DRIVERS[0]); i++)
		if (ksceUdcdGetDrvState(VIDEO_DRIVERS[i]) == SCE_UDCD_STATUS_DRIVER_STARTED)
			return 1;
	return 0;
}

static int on_the_dock(void)
{
	int s = ksceUdcdGetDeviceState();
	if (s < 0 || (s & SCE_UDCD_STATUS_CABLE_DISCONNECTED))
		return 0;
	if (!(s & SCE_UDCD_STATUS_CABLE_CONNECTED) || !(s & SCE_UDCD_STATUS_CONNECTION_ESTABLISHED))
		return 0;
	return video_plugin_running();
}

static int keep_off(void)
{
	return holding && on_the_dock();
}

static int hook_off(void)
{
	SceUInt32 now = panel_get();
	int ret = TAI_CONTINUE(int, hook_off_ref);

	if (!holding && on_the_dock()) {
		if (now)
			restore = now;
		holding = 1;
	}
	return ret;
}

static int hook_on(void)
{
	if (keep_off())
		return 0;
	return TAI_CONTINUE(int, hook_on_ref);
}

static int hook_set(SceUInt32 brightness)
{
	if (brightness && !holding)
		restore = brightness;
	if (brightness && keep_off()) {
		restore = brightness;
		return 0;
	}
	return TAI_CONTINUE(int, hook_set_ref, brightness);
}

static int watch(SceSize args, void *argp)
{
	(void)args;
	(void)argp;
	while (running) {
		if (!holding && panel_get() == 0 && on_the_dock()) {
			holding = 1;
		}
		if (holding) {
			if (!on_the_dock()) {
				holding = 0;
				panel_on();
				panel_set(restore ? restore : FALLBACK_BRIGHTNESS);
			} else if (panel_get() != 0) {
				panel_off();
			}
		}
		ksceKernelDelayThread(1000 * 1000);
	}
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

	if (find_panel() < 0)
		return SCE_KERNEL_START_FAILED;

	hook_off_id = taiHookFunctionExportForKernel(KERNEL_PID, &hook_off_ref, panel->module,
	                                             panel->lib, panel->nid_off, hook_off);
	hook_on_id = taiHookFunctionExportForKernel(KERNEL_PID, &hook_on_ref, panel->module,
	                                            panel->lib, panel->nid_on, hook_on);
	hook_set_id = taiHookFunctionExportForKernel(KERNEL_PID, &hook_set_ref, panel->module,
	                                             panel->lib, panel->nid_set, hook_set);
	if (hook_off_id < 0 || hook_on_id < 0 || hook_set_id < 0) {
		unhook();
		return SCE_KERNEL_START_FAILED;
	}

	running = 1;
	watcher = ksceKernelCreateThread("vitadock_screen", watch, 0x3C, 0x1000, 0, 0, NULL);
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
	if (holding) {
		holding = 0;
		panel_on();
		panel_set(restore ? restore : FALLBACK_BRIGHTNESS);
	}
	return SCE_KERNEL_STOP_SUCCESS;
}

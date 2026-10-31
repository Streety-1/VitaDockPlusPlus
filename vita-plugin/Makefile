TARGET  = vitadock
OBJS    = vitadock.o
LIBS    = -lSceThreadmgrForDriver_stub -lSceUdcdForDriver_stub \
          -ltaihenForKernel_stub -ltaihenModuleUtils_stub

PREFIX  = arm-vita-eabi
CC      = $(PREFIX)-gcc
CFLAGS  = -std=gnu11 -Wl,-q -Wall -Wextra -O2 -nostartfiles -mcpu=cortex-a9 -mthumb-interwork
LDFLAGS = -Wl,-Ttext-segment=0x0

all: $(TARGET).skprx

%.skprx: %.velf
	vita-make-fself -c $< $@

%.velf: %.elf
	vita-elf-create -e $(TARGET).yml $< $@

$(TARGET).elf: $(OBJS)
	$(CC) $(CFLAGS) $^ -o $@ $(LDFLAGS) $(LIBS)

%.o: %.c
	$(CC) $(CFLAGS) -c $< -o $@

clean:
	rm -f *.o *.elf *.velf *.skprx

.PHONY: all clean

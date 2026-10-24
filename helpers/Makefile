# Usage: make PJDIR=/absolute/path/to/configured/pjproject OUTPUT=/tmp/sipling-stream
include $(PJDIR)/build.mak
OUTPUT ?= /tmp/sipling-stream
all:
	$(APP_CC) $(APP_CFLAGS) -Wall -Wextra -Werror stream.c -o $(OUTPUT) $(APP_LDFLAGS) $(APP_LDLIBS)

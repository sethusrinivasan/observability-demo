CC ?= gcc
PG_CFLAGS ?= $(shell pkg-config --cflags libpq 2>/dev/null || echo "-I/usr/include/postgresql")
PG_LDFLAGS ?= $(shell pkg-config --libs libpq 2>/dev/null || echo "-lpq")
CURL_CFLAGS ?= $(shell pkg-config --cflags libcurl 2>/dev/null)
CURL_LDFLAGS ?= $(shell pkg-config --libs libcurl 2>/dev/null || echo "-lcurl")

CFLAGS ?= -O2 -Wall -Wextra -pthread $(PG_CFLAGS) $(CURL_CFLAGS)
LDFLAGS ?= -pthread $(PG_LDFLAGS) $(CURL_LDFLAGS) -lm

SRCS = main.c evaluator.c fibonacci.c cJSON.c
TARGET = server

all: $(TARGET)

$(TARGET): $(SRCS)
	$(CC) $(CFLAGS) -o $(TARGET) $(SRCS) $(LDFLAGS)

clean:
	rm -f $(TARGET) *.o

.PHONY: all clean

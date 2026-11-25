/* Windows host shim for the unchanged BSDIFF40 encoder and patch loop. */
#ifndef WORKHORSES_ERR_H
#define WORKHORSES_ERR_H

#include <errno.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void errx(int status, const char *format, ...)
{
    va_list args;
    va_start(args, format);
    if (format) vfprintf(stderr, format, args);
    va_end(args);
    fputc('\n', stderr);
    exit(status);
}

static void err(int status, const char *format, ...)
{
    int saved_errno = errno;
    va_list args;
    va_start(args, format);
    if (format) {
        vfprintf(stderr, format, args);
        fputs(": ", stderr);
    }
    va_end(args);
    fprintf(stderr, "%s\n", strerror(saved_errno));
    exit(status);
}

#endif

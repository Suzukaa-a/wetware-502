/* cortex-daemon-pin: start a long-lived in-story daemon on a fixed PID.
 *
 * The chamber's control plane is bound to specific pids in the fiction
 * (inquisitor_daemon=6660, agent #84's worker=8484) and operators read
 * /proc/<pid>.  A plain fork() lands on whatever pid is free, so this launcher
 * writes ns_last_pid and retries until the child really owns the wanted pid,
 * then execs the daemon's own binary in place.  The exec keeps the pinned pid
 * and makes /proc/<pid>/exe, /proc/<pid>/comm and /proc/<pid>/cmdline agree:
 * exe is the daemon path, comm is its basename (kernel-truncated to 15 bytes),
 * argv is that path plus the unit's arguments.
 *
 * usage: cortex-daemon-pin <pid> <pidfile> <program> [args...]
 */
#define _GNU_SOURCE
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

static int set_last_pid(long value)
{
    FILE *fp = fopen("/proc/sys/kernel/ns_last_pid", "w");

    if (!fp)
        return -1;
    fprintf(fp, "%ld", value - 1);
    fclose(fp);
    return 0;
}

static void write_pidfile(const char *path, long pid)
{
    FILE *fp = fopen(path, "w");

    if (!fp)
        return;
    fprintf(fp, "%ld\n", pid);
    fclose(fp);
}

int main(int argc, char **argv)
{
    long wanted;
    char *end = NULL;
    int attempt;
    const char *pidfile, *program;

    if (argc < 4) {
        fprintf(stderr, "usage: %s <pid> <pidfile> <program> [args...]\n",
                argv[0]);
        return 64;
    }
    wanted = strtol(argv[1], &end, 10);
    if (!end || *end || wanted < 2 || wanted > 4000000)
        return 64;
    pidfile = argv[2];
    program = argv[3];

    for (attempt = 0; attempt < 4000; attempt++) {
        pid_t pid;

        if (set_last_pid(wanted) != 0)
            return 70;
        pid = fork();
        if (pid < 0)
            return 70;
        if (pid == 0) {
            /* argv[0] is the program path: /proc/self/cmdline then reads like
             * the daemon unit line, and execve() renames comm to its basename.
             */
            execv(program, &argv[3]);
            _exit(127);
        }
        if ((long)pid == wanted) {
            write_pidfile(pidfile, (long)pid);
            return 0;
        }
        kill(pid, SIGKILL);
        waitpid(pid, NULL, 0);
    }
    fprintf(stderr, "cortex-daemon-pin: could not obtain pid %ld\n", wanted);
    return 70;
}

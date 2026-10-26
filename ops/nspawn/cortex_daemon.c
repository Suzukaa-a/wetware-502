/* cortex-daemon-pin: start a long-lived in-story daemon on a fixed PID.
 *
 * The chamber's control plane is bound to specific pids in the fiction
 * (inquisitor_daemon=6660, agent #84's worker=8484) and operators read
 * /proc/<pid>.  A plain fork() lands on whatever pid is free, so this launcher
 * writes ns_last_pid and retries until the child really owns the wanted pid.
 * The child then rewrites its own comm and argv image before parking, so
 * ps/top and /proc/<pid>/cmdline agree with the story.
 *
 * usage: cortex-daemon-pin <pid> <pidfile> <comm> <title>
 */
#define _GNU_SOURCE
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <unistd.h>

static void set_identity(const char *comm, const char *title)
{
    char argv[1024];
    FILE *stat;
    char raw[4096];
    long start = 0, end = 0;
    char *close_paren;
    int field;
    char *token;

    prctl(PR_SET_NAME, comm, 0, 0, 0);

    stat = fopen("/proc/self/stat", "rb");
    if (!stat)
        return;
    if (!fgets(raw, sizeof(raw), stat)) {
        fclose(stat);
        return;
    }
    fclose(stat);
    close_paren = strrchr(raw, ')');
    if (!close_paren)
        return;
    token = strtok(close_paren + 2, " ");
    for (field = 3; token; field++, token = strtok(NULL, " ")) {
        if (field == 48)
            start = strtol(token, NULL, 10);
        else if (field == 49) {
            end = strtol(token, NULL, 10);
            break;
        }
    }
    if (start <= 0 || end <= start)
        return;
    snprintf(argv, sizeof(argv), "%s", title);
    {
        size_t room = (size_t)(end - start);
        memset(argv + strlen(argv), 0, room > sizeof(argv) ? 0 : sizeof(argv) - strlen(argv));
        {
            FILE *mem = fopen("/proc/self/mem", "r+b");
            if (!mem)
                return;
            fseek(mem, start, SEEK_SET);
            fwrite(argv, 1, room < sizeof(argv) ? room : sizeof(argv), mem);
            fclose(mem);
        }
    }
}

static int set_last_pid(long value)
{
    FILE *fp = fopen("/proc/sys/kernel/ns_last_pid", "w");
    if (!fp)
        return -1;
    fprintf(fp, "%ld", value - 1);
    fclose(fp);
    return 0;
}

int main(int argc, char **argv)
{
    long wanted;
    char *end = NULL;
    int attempt;
    const char *pidfile, *comm, *title;

    if (argc < 5) {
        fprintf(stderr, "usage: %s <pid> <pidfile> <comm> <title>\n", argv[0]);
        return 64;
    }
    wanted = strtol(argv[1], &end, 10);
    if (!end || *end || wanted < 2 || wanted > 4000000)
        return 64;
    pidfile = argv[2];
    comm = argv[3];
    title = argv[4];

    for (attempt = 0; attempt < 4000; attempt++) {
        pid_t pid;

        if (set_last_pid(wanted) != 0)
            return 70;
        pid = fork();
        if (pid < 0)
            return 70;
        if (pid == 0) {
            set_identity(comm, title);
            for (;;)
                pause();
            _exit(0);
        }
        if ((long)pid == wanted) {
            FILE *fp = fopen(pidfile, "w");
            if (fp) {
                fprintf(fp, "%ld\n", (long)pid);
                fclose(fp);
            }
            return 0;
        }
        kill(pid, SIGKILL);
        waitpid(pid, NULL, 0);
    }
    fprintf(stderr, "cortex-daemon-pin: could not obtain pid %ld\n", wanted);
    return 70;
}

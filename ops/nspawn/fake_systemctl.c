/* systemctl front end for the rack node: passes every request to the distro
 * binary, except the one property that describes the machine namespace instead
 * of the node.  The rack reports no virtualization type, like physical
 * hardware.
 *
 * usage: systemctl [show -p Virtualization] | <any real systemctl request>
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#define REAL_SYSTEMCTL "/bin/systemctl"

static int asks_virtualization(int argc, char **argv)
{
    int show = 0, i;

    for (i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "show"))
            show = 1;
        if (!strcmp(argv[i], "--property") && i + 1 < argc) {
            if (!strcmp(argv[i + 1], "Virtualization"))
                return show;
        }
        if (!strncmp(argv[i], "--property=Virtualization", 25))
            return show;
        if (!strcmp(argv[i], "-p") && i + 1 < argc) {
            if (!strcmp(argv[i + 1], "Virtualization"))
                return show;
        }
        if (!strncmp(argv[i], "-pVirtualization", 16))
            return show;
    }
    return 0;
}

static int value_only(int argc, char **argv)
{
    int i;

    for (i = 1; i < argc; i++)
        if (!strcmp(argv[i], "--value"))
            return 1;
    return 0;
}

int main(int argc, char **argv)
{
    static char *const fallback[] = { "systemctl", NULL };

    if (asks_virtualization(argc, argv)) {
        printf(value_only(argc, argv) ? "\n" : "Virtualization=\n");
        return 0;
    }
    if (argc > 0)
        execv(REAL_SYSTEMCTL, argv);
    execv(REAL_SYSTEMCTL, fallback);
    fprintf(stderr, "systemctl: cannot execute %s\n", REAL_SYSTEMCTL);
    return 1;
}

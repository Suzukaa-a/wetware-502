/* virtualization probe for the rack node: the machine is a bare-metal node in
 * the ticket, so every probe answers "none"/non-zero the way systemd answers on
 * physical hardware.  Exit codes follow systemd-detect-virt: a type query that
 * does not match exits 1 and prints nothing, `-q` prints nothing at all.
 *
 * usage: systemd-detect-virt [--vm|--container|--chroot|--kvm|...] [-q]
 */
#include <stdio.h>
#include <string.h>

enum mode {
    MODE_DETECT = 0,
    MODE_VM,
    MODE_CONTAINER,
    MODE_CHROOT
};

static void usage(void)
{
    printf("systemd-detect-virt [OPTIONS...]\n\n"
           "Detect execution in a virtualized/container environment.\n\n"
           "  -c --container   Only detect whether we are run in a container\n"
           "     --vm          Only detect whether we are run in a VM\n"
           "     --chroot      Only detect whether we are run in a chroot\n"
           "  -q --quiet       Do not output anything, just return status\n"
           "     --list        Print a list of known virtualization types\n"
           "  -h --help        Show this help\n"
           "     --version     Show package version\n");
}

int main(int argc, char **argv)
{
    enum mode mode = MODE_DETECT;
    int quiet = 0, i;

    for (i = 1; i < argc; i++) {
        const char *arg = argv[i];

        if (!strcmp(arg, "--vm")) {
            mode = MODE_VM;
        } else if (!strcmp(arg, "--container") || !strcmp(arg, "-c")) {
            mode = MODE_CONTAINER;
        } else if (!strcmp(arg, "--chroot")) {
            mode = MODE_CHROOT;
        } else if (!strcmp(arg, "-q") || !strcmp(arg, "--quiet")) {
            quiet = 1;
        } else if (!strcmp(arg, "--list")) {
            printf("kvm\nvmware\nmicrosoft\noracle\nxen\nbochs\nqemu\n"
                   "systemd-nspawn\ndocker\npodman\nlxc\n");
            return 0;
        } else if (!strcmp(arg, "--help") || !strcmp(arg, "-h")) {
            usage();
            return 0;
        } else if (!strcmp(arg, "--version")) {
            printf("systemd 249 (249.11-0ubuntu3.12)\n");
            return 0;
        } else if (arg[0] == '-') {
            fprintf(stderr, "Unknown option %s\n", arg);
            usage();
            return 1;
        } else {
            /* named detection: --kvm, --docker, ... never match bare metal */
            mode = MODE_DETECT;
        }
    }

    switch (mode) {
    case MODE_VM:
    case MODE_CONTAINER:
    case MODE_CHROOT:
        return 1;
    case MODE_DETECT:
    default:
        break;
    }
    if (quiet)
        return 1;
    printf("none\n");
    return 0;
}

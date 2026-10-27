/* hostnamectl: node identity report for a rack server.
 *
 * The guest runs inside an nspawn machine but the operator-visible answer must
 * describe the node the ticket is about: a bare-metal server chassis, no
 * hypervisor hint.  Reads the same files the distro tool reads (/etc/hostname,
 * /etc/machine-id, /etc/machine-info, /etc/os-release,
 * /proc/sys/kernel/random/boot_id) and prints the real kernel release from
 * uname(2), so the report agrees with `uname -r`, /var/log/kern.log and
 * /var/log/dmesg.  The field set, order and column alignment follow the
 * stock systemd 249 output for a server chassis:
 *
 *   Static hostname / Icon name / Chassis / Machine ID / Boot ID /
 *   Virtualization / Operating System / Kernel / Architecture
 *
 * /etc/machine-info may carry DEPLOYMENT=; the distro tool only shows that
 * value when asked for it explicitly (--deployment), so the status block does
 * not print a Deployment line.
 *
 * usage: hostnamectl [status|--static|--transient|--pretty|--hostname|
 *                     --machine-id|--boot-id|--chassis|--icon-name|
 *                     --deployment|--version]
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <string.h>
#include <sys/utsname.h>
#include <unistd.h>

static void read_first_line(const char *path, char *out, size_t size,
                            const char *fallback)
{
    FILE *fp = fopen(path, "r");
    size_t len;

    snprintf(out, size, "%s", fallback);
    if (!fp)
        return;
    if (fgets(out, (int)size, fp)) {
        len = strlen(out);
        while (len && (out[len - 1] == '\n' || out[len - 1] == '\r'))
            out[--len] = '\0';
    }
    fclose(fp);
}

/* KEY=VALUE lookup in a key/value file (os-release, machine-info). */
static void read_key(const char *path, const char *key, char *out, size_t size)
{
    char line[512];
    size_t keylen = strlen(key);
    FILE *fp = fopen(path, "r");

    out[0] = '\0';
    if (!fp)
        return;
    while (fgets(line, sizeof(line), fp)) {
        char *value, *end;

        if (strncmp(line, key, keylen) || line[keylen] != '=')
            continue;
        value = line + keylen + 1;
        if (*value == '"')
            value++;
        end = value + strlen(value);
        while (end > value && (end[-1] == '\n' || end[-1] == '\r' ||
                               end[-1] == '"'))
            *--end = '\0';
        snprintf(out, size, "%s", value);
        break;
    }
    fclose(fp);
}

static void hostname_value(char *out, size_t size)
{
    struct utsname uts;

    if (uname(&uts) == 0 && uts.nodename[0]) {
        snprintf(out, size, "%s", uts.nodename);
        return;
    }
    read_first_line("/etc/hostname", out, size, "localhost");
}

static void kernel_value(char *out, size_t size)
{
    struct utsname uts;

    if (uname(&uts) == 0 && uts.release[0]) {
        snprintf(out, size, "%s", uts.release);
        return;
    }
    snprintf(out, size, "5.15.0-198-generic");
}

static void report(void)
{
    char host[256], machine_id[64], boot_id[64], kernel[128];
    char os_pretty[128];

    hostname_value(host, sizeof(host));
    read_first_line("/etc/machine-id", machine_id, sizeof(machine_id), "");
    read_first_line("/proc/sys/kernel/random/boot_id", boot_id, sizeof(boot_id),
                    "");
    kernel_value(kernel, sizeof(kernel));
    read_key("/etc/os-release", "PRETTY_NAME", os_pretty, sizeof(os_pretty));
    if (!os_pretty[0])
        snprintf(os_pretty, sizeof(os_pretty), "Ubuntu 22.04 LTS");

    printf(" Static hostname: %s\n", host);
    printf("       Icon name: computer-server\n");
    printf("         Chassis: server\n");
    if (machine_id[0])
        printf("      Machine ID: %s\n", machine_id);
    if (boot_id[0])
        printf("         Boot ID: %s\n", boot_id);
    printf("  Virtualization: none\n");
    printf("Operating System: %s\n", os_pretty);
    printf("          Kernel: Linux %s\n", kernel);
    printf("    Architecture: x86-64\n");
}

int main(int argc, char **argv)
{
    char value[256];
    int i;

    for (i = 1; i < argc; i++) {
        const char *arg = argv[i];

        if (!strcmp(arg, "status"))
            continue;
        if (!strcmp(arg, "--static") || !strcmp(arg, "--transient") ||
            !strcmp(arg, "--pretty") || !strcmp(arg, "--hostname")) {
            hostname_value(value, sizeof(value));
            printf("%s\n", value);
            return 0;
        }
        if (!strcmp(arg, "--machine-id")) {
            read_first_line("/etc/machine-id", value, sizeof(value), "");
            printf("%s\n", value);
            return 0;
        }
        if (!strcmp(arg, "--boot-id")) {
            read_first_line("/proc/sys/kernel/random/boot_id", value,
                            sizeof(value), "");
            printf("%s\n", value);
            return 0;
        }
        if (!strcmp(arg, "--chassis")) {
            printf("server\n");
            return 0;
        }
        if (!strcmp(arg, "--icon-name")) {
            printf("computer-server\n");
            return 0;
        }
        if (!strcmp(arg, "--deployment")) {
            read_key("/etc/machine-info", "DEPLOYMENT", value, sizeof(value));
            if (value[0])
                printf("%s\n", value);
            return 0;
        }
        if (!strcmp(arg, "--version")) {
            printf("systemd 249 (249.11-0ubuntu3.12)\n");
            printf("+PAM +AUDIT +SELINUX +APPARMOR +IMA +SMACK +SECCOMP "
                   "+GCRYPT +OPENSSL\n");
            return 0;
        }
        if (!strcmp(arg, "--help") || !strcmp(arg, "-h")) {
            printf("hostnamectl [OPTIONS...] COMMAND ...\n\n"
                   "Query or change system hostname.\n");
            return 0;
        }
    }
    report();
    return 0;
}

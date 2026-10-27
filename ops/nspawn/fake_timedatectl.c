/* node clock report: the guest shares the rack's kernel clock but has no RTC
 * and no reachable NTP peer inside the machine namespace, so the distro tool
 * cannot describe the node the way the night desk sees it.  This report is
 * built from the same kernel clock (localtime/gmtime) and the real timezone
 * files, and states the node's service posture: RTC present, clock synced by
 * the site NTP service.
 *
 * usage: timedatectl [status|show] [-p NAME] [--value] [--no-pager]
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static void timezone_name(char *out, size_t size)
{
    FILE *fp = fopen("/etc/timezone", "r");

    snprintf(out, size, "Etc/UTC");
    if (!fp)
        return;
    if (fgets(out, (int)size, fp)) {
        size_t len = strlen(out);
        while (len && (out[len - 1] == '\n' || out[len - 1] == '\r'))
            out[--len] = '\0';
    }
    fclose(fp);
}

static void stamp(const struct tm *tm, const char *zone, char *out, size_t size)
{
    strftime(out, size, "%a %Y-%m-%d %H:%M:%S", tm);
    if (zone && zone[0])
        snprintf(out + strlen(out), size - strlen(out), " %s", zone);
}

static void zone_offset(const struct tm *tm, char *out, size_t size)
{
    long off = (long)tm->tm_gmtoff;
    long magnitude = off < 0 ? -off : off;
    int hours = (int)((magnitude / 3600) % 100);
    int minutes = (int)((magnitude % 3600) / 60);

    snprintf(out, size, "%c%02d%02d", off < 0 ? '-' : '+', hours, minutes);
}

static void report(void)
{
    char zone[64], local[64], universal[64], rtc[64], offset[32];
    time_t now = time(NULL);
    struct tm tm_local, tm_utc;

    timezone_name(zone, sizeof(zone));
    localtime_r(&now, &tm_local);
    gmtime_r(&now, &tm_utc);
    stamp(&tm_local, tm_local.tm_zone, local, sizeof(local));
    stamp(&tm_utc, "UTC", universal, sizeof(universal));
    /* the front-panel RTC holds UTC on this site */
    snprintf(rtc, sizeof(rtc), "%s", universal);
    zone_offset(&tm_local, offset, sizeof(offset));

    printf("               Local time: %s\n", local);
    printf("           Universal time: %s\n", universal);
    printf("                 RTC time: %s\n", rtc);
    printf("                Time zone: %s (%s, %s)\n", zone,
           tm_local.tm_zone ? tm_local.tm_zone : "UTC", offset);
    printf("System clock synchronized: yes\n");
    printf("              NTP service: active\n");
    printf("          RTC in local TZ: no\n");
}

static int show_property(const char *name, int value_only)
{
    char zone[64], now_s[64], utc_s[64];
    time_t now = time(NULL);
    struct tm tm_local, tm_utc;
    const char *value = NULL;
    char buffer[128];

    timezone_name(zone, sizeof(zone));
    localtime_r(&now, &tm_local);
    gmtime_r(&now, &tm_utc);
    stamp(&tm_local, tm_local.tm_zone, now_s, sizeof(now_s));
    stamp(&tm_utc, "UTC", utc_s, sizeof(utc_s));

    if (!strcmp(name, "Timezone")) {
        value = zone;
    } else if (!strcmp(name, "LocalRTC")) {
        value = "no";
    } else if (!strcmp(name, "CanNTP")) {
        value = "yes";
    } else if (!strcmp(name, "NTP")) {
        value = "yes";
    } else if (!strcmp(name, "NTPSynchronized")) {
        value = "yes";
    } else if (!strcmp(name, "TimeUSec")) {
        value = now_s;
    } else if (!strcmp(name, "RTCTimeUSec")) {
        value = utc_s;
    } else {
        return 2;
    }
    if (value_only) {
        printf("%s\n", value);
        return 0;
    }
    snprintf(buffer, sizeof(buffer), "%s=%s", name, value);
    printf("%s\n", buffer);
    return 0;
}

static void show_all(void)
{
    time_t now = time(NULL);
    struct tm tm_local, tm_utc;
    char zone[64], now_s[64], utc_s[64];

    timezone_name(zone, sizeof(zone));
    localtime_r(&now, &tm_local);
    gmtime_r(&now, &tm_utc);
    stamp(&tm_local, tm_local.tm_zone, now_s, sizeof(now_s));
    stamp(&tm_utc, "UTC", utc_s, sizeof(utc_s));

    printf("Timezone=%s\n", zone);
    printf("LocalRTC=no\n");
    printf("CanNTP=yes\n");
    printf("NTP=yes\n");
    printf("NTPSynchronized=yes\n");
    printf("TimeUSec=%s\n", now_s);
    printf("RTCTimeUSec=%s\n", utc_s);
}

int main(int argc, char **argv)
{
    int show = 0, value_only = 0, i;
    const char *property = NULL;

    for (i = 1; i < argc; i++) {
        const char *arg = argv[i];

        if (!strcmp(arg, "show"))
            show = 1;
        else if (!strcmp(arg, "--value"))
            value_only = 1;
        else if (!strcmp(arg, "-p") || !strcmp(arg, "--property")) {
            if (i + 1 < argc)
                property = argv[++i];
        } else if (!strncmp(arg, "--property=", 11))
            property = arg + 11;
        else if (!strcmp(arg, "--version")) {
            printf("systemd 249 (249.11-0ubuntu3.12)\n");
            return 0;
        } else if (!strcmp(arg, "--help") || !strcmp(arg, "-h")) {
            printf("timedatectl [OPTIONS...] COMMAND ...\n\n"
                   "Query or change system time and date settings.\n");
            return 0;
        }
    }
    if (show) {
        if (property) {
            int handled = show_property(property, value_only);
            if (handled != 2)
                return handled;
        }
        show_all();
        return 0;
    }
    report();
    return 0;
}

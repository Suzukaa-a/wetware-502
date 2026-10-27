/* pinned node daemon: the long-lived body of a fixed-PID service process.
 *
 * The unit starts cortex-daemon-pin, which pins the child to the PID the
 * tickets name and execs this binary in place, so /proc/<pid>/exe,
 * /proc/<pid>/comm and /proc/<pid>/cmdline all describe one real program:
 *   exe   = the title path the launcher exec'd (.. /usr/sbin/inquisitor_daemon)
 *   comm  = basename of that path, truncated to 15 bytes by the kernel
 *   argv  = that path plus the unit's arguments
 * The daemon holds the process open; the parent writes the PID file.
 */
#include <signal.h>
#include <unistd.h>

int main(void)
{
    for (;;)
        pause();
    return 0;
}

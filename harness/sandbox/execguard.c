/* I-34 exec-stage guard: Landlock restrict then exec.
 * argv: execguard [rw PATH|ro PATH]... -- CMD [ARG]...
 * Base RO system mounts (/usr /lib64 /lib /bin /etc /proc /dev) that exist
 * inside the sandbox are auto-added read-only so the caller only declares
 * the workspace domain.  Restrict failure => exit 2, stderr prefixed with
 * "execguard:" (fail-closed marker for the caller).
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/prctl.h>
#include <sys/syscall.h>

#ifndef __NR_landlock_create_ruleset
#define __NR_landlock_create_ruleset 444
#define __NR_landlock_add_rule 445
#define __NR_landlock_restrict_self 446
#endif
#define LANDLOCK_RULE_PATH_BENEATH 1
struct ll_ruleset { unsigned long long handled_access_fs; };
struct ll_path { unsigned long long allowed_access; int parent_fd; }
  __attribute__((packed));
#define FS_ALL ((1ULL << 15) - 1)
#define FS_RO (1ULL << 0 | 1ULL << 2 | 1ULL << 3)

static const char *BASE_RO[] = { "/usr", "/lib64", "/lib", "/bin", "/etc",
                                 "/proc", "/dev", 0 };
static int excluded(const char *path, int argc, char **argv) {
  int i;
  for (i = 1; i < argc; i++) {
    if (!strcmp(argv[i], "roex")) {
      i++;
      if (i < argc && !strcmp(argv[i], path)) return 1;
    } else if (!strcmp(argv[i], "--")) {
      break;
    }
  }
  return 0;
}

static int add_rule(int fd, unsigned long long acc, const char *path) {
  int pfd = open(path, O_PATH | O_CLOEXEC);
  if (pfd < 0) { fprintf(stderr, "execguard: open %s\n", path); return -1; }
  struct ll_path pr = { acc, pfd };
  int rc = (int)syscall(__NR_landlock_add_rule, fd, LANDLOCK_RULE_PATH_BENEATH,
                        &pr, 0);
  close(pfd);
  if (rc) { fprintf(stderr, "execguard: add_rule %s\n", path); }
  return rc;
}

static int declared(const char *path, int argc, char **argv) {
  int i;
  for (i = 1; i < argc; i++) {
    if (!strcmp(argv[i], "--")) break;
    i++; /* mode */
    if (i < argc && !strcmp(argv[i], path)) return 1;
  }
  return 0;
}

int main(int argc, char **argv) {
  int i;
  prctl(38, 1); /* no_new_privs */
  struct ll_ruleset rs = { FS_ALL };
  int fd = (int)syscall(__NR_landlock_create_ruleset, &rs, sizeof(rs), 0);
  if (fd < 0) { fprintf(stderr, "execguard: create_ruleset\n"); return 2; }

  /* base RO sys dirs first (skip any the caller already declares) */
  int b;
  for (b = 0; BASE_RO[b]; b++) {
    if (access(BASE_RO[b], F_OK) == 0
        && !declared(BASE_RO[b], argc, argv)
        && !excluded(BASE_RO[b], argc, argv)) {
      if (add_rule(fd, FS_RO, BASE_RO[b])) return 2;
    }
  }
  for (i = 1; i < argc; i++) {
    if (!strcmp(argv[i], "--")) break;
    if (!strcmp(argv[i], "roex")) { i++; continue; }
    const char *mode = argv[i]; i++;
    if (i >= argc) { fprintf(stderr, "execguard: missing path\n"); return 2; }
    unsigned long long acc = !strcmp(mode, "rw") ? FS_ALL : FS_RO;
    if (add_rule(fd, acc, argv[i])) return 2;
  }
  if (syscall(__NR_landlock_restrict_self, fd, 0)) {
    fprintf(stderr, "execguard: restrict_self\n"); return 2;
  }
  if (i >= argc) return 0;
  execvp(argv[i + 1], &argv[i + 1]);
  fprintf(stderr, "execguard: execvp\n");
  return 2;
}

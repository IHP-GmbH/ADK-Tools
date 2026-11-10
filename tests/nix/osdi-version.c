/* SPDX-License-Identifier: Apache-2.0
 * SPDX-FileCopyrightText: 2026 IHP GmbH
 *
 * Prints the OSDI interface version an OSDI model file declares, read from its
 * OSDI_VERSION_MAJOR and OSDI_VERSION_MINOR symbols, as "<file> osdi <major>.<minor>".
 * Exits 1 if a file cannot be opened or lacks either symbol.
 */
#include <dlfcn.h>
#include <stdint.h>
#include <stdio.h>

int main(int argc, char **argv)
{
    int rc = 0;
    for (int i = 1; i < argc; i++) {
        void *h = dlopen(argv[i], RTLD_NOW | RTLD_LOCAL);
        if (!h) {
            fprintf(stderr, "%s: %s\n", argv[i], dlerror());
            rc = 1;
            continue;
        }
        const uint32_t *major = dlsym(h, "OSDI_VERSION_MAJOR");
        const uint32_t *minor = dlsym(h, "OSDI_VERSION_MINOR");
        if (!major || !minor) {
            fprintf(stderr, "%s: no OSDI_VERSION_MAJOR/OSDI_VERSION_MINOR\n", argv[i]);
            rc = 1;
        } else {
            printf("%s osdi %u.%u\n", argv[i], *major, *minor);
        }
        dlclose(h);
    }
    return rc;
}

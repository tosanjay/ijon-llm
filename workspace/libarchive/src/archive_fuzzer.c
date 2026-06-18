/*
 * libarchive read-path fuzz harness (IJON-Reloaded real-target walkthrough).
 *
 * Decodes an arbitrary archive: every filter (gzip/xz/zstd/...) and every format
 * (tar/cpio/zip/7z/...) is enabled, then we walk the entries and drain each one.
 * This is the classic libarchive_fuzzer (contrib/oss-fuzz) shape, self-contained
 * (no fuzz_helpers.h) and driven by libFuzzer (-fsanitize=fuzzer).
 *
 * The interesting *state* here is invisible to edge coverage: the SEQUENCE of
 * (container format, per-entry file type) the decoder walks through. Two inputs
 * that hit the same code in a different order look identical to AFL's edge map.
 * That is exactly the gap IJON closes -- see the reference annotation below,
 * which the fairness gate strips before the agent ever sees this file.
 */
#include <stddef.h>
#include <stdint.h>
#include <archive.h>
#include <archive_entry.h>

struct Buffer { const uint8_t *data; size_t size; size_t pos; };

static la_ssize_t reader_callback(struct archive *a, void *client_data,
                                  const void **buf) {
    (void)a;
    struct Buffer *b = (struct Buffer *)client_data;
    if (b->pos >= b->size) return 0;          /* EOF */
    *buf = b->data + b->pos;
    size_t remaining = b->size - b->pos;
    b->pos = b->size;                          /* hand over all remaining bytes */
    return (la_ssize_t)remaining;
}

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size) {
    struct archive *a = archive_read_new();
    archive_read_support_filter_all(a);
    archive_read_support_format_all(a);

    struct Buffer b = { data, size, 0 };
    archive_read_open(a, &b, NULL, reader_callback, NULL);

    char block[4096];
    struct archive_entry *entry;
    for (;;) {
        int r = archive_read_next_header(a, &entry);
        if (r == ARCHIVE_EOF || r == ARCHIVE_FATAL) break;
        if (r == ARCHIVE_RETRY) continue;

        /* IJON-ANCHOR: one decoded entry header is one state transition. */
#ifdef _USE_IJON
        IJON_STATE(ijon_hashint(
            ijon_hashint((uint32_t)archive_filter_code(a, 0),
                         (uint32_t)archive_format(a)),
            (uint32_t)archive_entry_filetype(entry)));
#endif
        la_ssize_t n;
        while ((n = archive_read_data(a, block, sizeof block)) > 0)
            ;
        if (n == ARCHIVE_FATAL) break;
    }

    archive_read_free(a);
    return 0;
}

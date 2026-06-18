/*
 * archive_describe -- the class-2 metric extractor for the libarchive walkthrough.
 *
 * Reads one archive (argv[1]) exactly the way the fuzz harness does, and prints a
 * single line: the SEQUENCE of "<format-code>:<entry-filetype>" tokens the decoder
 * walks. Counting distinct lines across a corpus = distinct state sequences the
 * fuzzer reached -- the same class-2 "sequence diversity" metric the IJON paper
 * uses for stateful targets, and the reward run_target.py keeps/reverts on.
 *
 * Authoritative (uses libarchive itself), deterministic, and not IJON-instrumented.
 */
#include <stdio.h>
#include <stdlib.h>
#include <archive.h>
#include <archive_entry.h>

int main(int argc, char **argv) {
    if (argc < 2) { fprintf(stderr, "usage: %s <archive>\n", argv[0]); return 2; }

    struct archive *a = archive_read_new();
    archive_read_support_filter_all(a);
    archive_read_support_format_all(a);
    if (archive_read_open_filename(a, argv[1], 16384) != ARCHIVE_OK) {
        printf("OPEN_FAIL\n");                  /* a distinct, stable bucket */
        archive_read_free(a);
        return 0;
    }

    struct archive_entry *entry;
    int first = 1, r;
    while ((r = archive_read_next_header(a, &entry)) == ARCHIVE_OK) {
        printf("%s%d.%d:%d", first ? "" : " ",
               archive_filter_code(a, 0), archive_format(a),
               (int)archive_entry_filetype(entry));
        first = 0;
        archive_read_data_skip(a);
    }
    if (first) printf("EMPTY");                 /* opened but no entries */
    printf("\n");
    archive_read_free(a);
    return 0;
}

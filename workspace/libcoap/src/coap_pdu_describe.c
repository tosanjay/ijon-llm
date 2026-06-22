/*
 * coap_pdu_describe -- the class-2 metric extractor for the libcoap target.
 *
 * Reads one input (argv[1]) exactly the way the fuzz harness parses it
 * (coap_pdu_parse over COAP_PROTO_UDP) and prints a SINGLE line: the CoAP PDU's
 * coverage-blind structural state --
 *
 *     T<type>C<code>|<opt> <opt> <opt> ...
 *
 * i.e. the message type (CON/NON/ACK/RST), the code (method / response), and the
 * ORDERED sequence of option numbers. Option numbers are delta-encoded and order-
 * sensitive, so two PDUs with the same options in a different order run the SAME
 * parse branches -- edge coverage cannot tell them apart, but they are distinct
 * protocol states. Counting distinct lines across a corpus = distinct state
 * sequences the fuzzer reached: the class-2 "sequence diversity" reward
 * run_target.py keeps/reverts on (same metric the IJON paper uses for stateful
 * targets, same contract as workspace/libarchive/src/archive_describe.c).
 *
 * Authoritative (uses libcoap itself), deterministic, not IJON-instrumented.
 * Contract: print exactly one line to stdout; stable buckets for the edge cases.
 */
#include <stdio.h>
#include <stdlib.h>
#include "coap3/coap.h"

int main(int argc, char **argv) {
  if (argc < 2) { fprintf(stderr, "usage: %s <input>\n", argv[0]); return 2; }

  FILE *f = fopen(argv[1], "rb");
  if (!f) { printf("OPEN_FAIL\n"); return 0; }          /* distinct, stable bucket */
  fseek(f, 0, SEEK_END);
  long n = ftell(f);
  if (n < 0) { fclose(f); printf("OPEN_FAIL\n"); return 0; }
  fseek(f, 0, SEEK_SET);
  uint8_t *data = (uint8_t *)malloc(n ? (size_t)n : 1);
  if (!data) { fclose(f); printf("OPEN_FAIL\n"); return 0; }
  size_t size = fread(data, 1, (size_t)n, f);
  fclose(f);

  coap_startup();
  coap_set_log_level(COAP_LOG_EMERG);

  coap_pdu_t *pdu = coap_pdu_init(0, 0, 0, size);
  if (!pdu) { free(data); printf("INIT_FAIL\n"); coap_cleanup(); return 0; }

  if (!coap_pdu_parse(COAP_PROTO_UDP, data, size, pdu)) {
    printf("PARSE_FAIL\n");                              /* malformed -> one bucket */
  } else {
    printf("T%dC%d|", (int)coap_pdu_get_type(pdu), (int)coap_pdu_get_code(pdu));
    coap_opt_iterator_t oi;
    coap_opt_t *opt;
    coap_option_iterator_init(pdu, &oi, COAP_OPT_ALL);
    int first = 1, any = 0;
    while ((opt = coap_option_next(&oi))) {
      printf("%s%u", first ? "" : " ", (unsigned)oi.number);
      first = 0; any = 1;
    }
    if (!any) printf("-");                               /* parsed, no options */
    printf("\n");
  }

  coap_delete_pdu(pdu);
  free(data);
  coap_cleanup();
  return 0;
}

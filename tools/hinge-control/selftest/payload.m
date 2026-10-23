// Prints the payloads Sources/HingePayload.h builds, as hex, one per line:
// the hinge at 130 degrees and the picker at "portrait". Built for the host by
// scripts/hinge-selftest.sh, which compares them with the bytes that moved an
// iPhone Duo (Xcode 27.1 / iOS 27.1) -- the helper itself only runs inside a
// simulator, so this is how its encoder is checked anywhere else.
#import <Foundation/Foundation.h>
#import "../Sources/HingePayload.h"

static void printHex(NSData *d) {
  const uint8_t *b = d.bytes;
  for (NSUInteger i = 0; i < d.length; i++) printf("%02x", b[i]);
  printf("\n");
}

int main(void) {
  @autoreleasepool {
    printHex(hingePayload(130.0));
    printHex(orientationPayload("portrait"));
  }
  return 0;
}

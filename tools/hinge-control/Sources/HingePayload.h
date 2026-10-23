// HingePayload.h — the keyed record Device Hub dispatches on its `avpCustom`
// HID service to move iPhone Duo's hinge or orientation picker.
//
// Adapted for quern from tddworks/baguette, Injected/HingeControl/Sources/
// HingeControl.m (Apache-2.0; see ../LICENSE.baguette and ../README.md for the
// pinned commit). Split out of HingeControl.m, unchanged otherwise, so that
// scripts/hinge-selftest.sh can compile it for the host and check its bytes:
// the helper itself only runs inside a simulator.
//
// Record layout: `d3 00 00 00`, then items of [u24 aux][u8 type], the top bit
// of `type` marking a container's last entry. 0x01 dictionary (aux = entry
// count), 0x08 key (NUL-terminated, aux = length incl NUL), 0x09 string
// (aux = length), 0x04 double (aux = 0x3f, 8 bytes LE). Every item is padded
// to 4 bytes.
#pragma once
#import <Foundation/Foundation.h>
#include <string.h>

static void putHeader(NSMutableData *d, uint32_t aux, uint8_t type) {
  uint8_t h[4] = { aux & 0xff, (aux >> 8) & 0xff, (aux >> 16) & 0xff, type };
  [d appendBytes:h length:4];
}
static void pad4(NSMutableData *d) { while (d.length % 4) { uint8_t z = 0; [d appendBytes:&z length:1]; } }
static void putKey(NSMutableData *d, const char *k) {
  size_t n = strlen(k) + 1; putHeader(d, (uint32_t)n, 0x08); [d appendBytes:k length:n]; pad4(d);
}
static void putString(NSMutableData *d, const char *s, BOOL last) {
  size_t n = strlen(s); putHeader(d, (uint32_t)n, 0x09 | (last ? 0x80 : 0)); [d appendBytes:s length:n]; pad4(d);
}
static void putDouble(NSMutableData *d, double v, BOOL last) {
  putHeader(d, 0x3f, 0x04 | (last ? 0x80 : 0)); [d appendBytes:&v length:8];
}
static NSData *hingePayload(double degrees) {
  NSMutableData *d = [NSMutableData data];
  uint8_t magic[4] = { 0xd3, 0, 0, 0 }; [d appendBytes:magic length:4];
  putHeader(d, 4, 0x81);
  putKey(d, "provider"); putString(d, "com.apple.Virtualization.VirtualMachines", NO);
  putKey(d, "source");   putString(d, "hinge-slider-control", NO);
  putKey(d, "type");     putString(d, "range", NO);
  putKey(d, "value");    putDouble(d, degrees, YES);
  return d;
}
static NSData *orientationPayload(const char *value) {
  NSMutableData *d = [NSMutableData data];
  uint8_t magic[4] = { 0xd3, 0, 0, 0 }; [d appendBytes:magic length:4];
  putHeader(d, 4, 0x81);
  putKey(d, "provider"); putString(d, "com.apple.Virtualization.VirtualMachines", NO);
  putKey(d, "source");   putString(d, "orientation-picker-control", NO);
  putKey(d, "type");     putString(d, "enum", NO);
  putKey(d, "value");    putString(d, value, YES);
  return d;
}

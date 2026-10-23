// Adapted for quern from tddworks/baguette, Injected/HingeControl/Sources/
// HingeControl.m (Apache-2.0; see ../LICENSE.baguette). The pinned upstream
// commit and what was changed are in ../README.md and ../hinge-pin.json.
// Changes: the hardware-key service and `button` verb, and the `frontmost`
// query (idb-derived), are left out -- quern does not need them; the payload
// encoder moved to HingePayload.h so the host can test its bytes.
//
// HingeControl — drives iPhone Duo's hinge (and its orientation picker)
// from inside the simulator, the way Device Hub does.
//
// Device Hub speaks CoreDevice's UniversalHID to the guest daemon
// `dtuhidd`, which owns a virtual HID service it calls `avpCustom`
// (usage page 0xFF61, usage 0x5B) and dispatches every pose command on it
// as a vendor-defined IOHIDEvent whose payload is a keyed record:
//   {provider: "com.apple.Virtualization.VirtualMachines",
//    source: "hinge-slider-control", type: "range", value: <degrees>}
// for the hinge, and {source: "orientation-picker-control", type: "enum",
// value: "portrait"} for the picker. The runtime's consumer of those
// events does not care which process's service they came from — so this
// tool, spawned in the guest by `simctl spawn`, registers a service of the
// same shape with the private HID.framework and dispatches the same
// events. Measured against Device Hub with a HID event monitor in the
// guest; the encoder below reproduces its payload byte for byte.
//
// The record's layout is in HingePayload.h.
//
//   HingeControl angle <degrees>
//   HingeControl sweep <from> <to> <milliseconds>     (60 Hz, ease-out)
//   HingeControl orientation <portrait|pud|landscape-left|landscape-right>
//   HingeControl serve        — the same verbs, one per line on stdin,
//                               until EOF, each answered `done <status>`;
//                               baguette keeps one of these per device so
//                               a pose costs no spawn.
//   HingeControl read-angle   — prints the hinge's angle, `angle <degrees>`,
//                               and exits; reads only, no HID service.
//   HingeControl watch-angle  — prints every sample the same way until stopped.
//
// Any verb that acts may follow `--deadline <unix-seconds>`: past it, a helper
// not yet ready to act exits 3 without acting. The read-only queries
// (`read-angle`, `watch-angle`) take none; they move nothing. A helper prints `pid <n>` before it
// starts, so its owner can stop it after a timeout.
#import <Foundation/Foundation.h>
#import "HingeProtocol.h"
#import <dlfcn.h>
#import <objc/runtime.h>
#import <objc/message.h>
#import <mach/mach_time.h>
#import "HingeAngle.h"
#import "HingePayload.h"

typedef void *IOHIDEventRef;
static IOHIDEventRef (*IOHIDEventCreateVendorDefinedEvent)(CFAllocatorRef, uint64_t, uint32_t, uint32_t, uint32_t, uint8_t *, CFIndex, uint32_t);

// --- the virtual service's delegate -------------------------------------
@interface ServiceDelegate : NSObject
@property (nonatomic, strong) NSDictionary *properties;
@end
@implementation ServiceDelegate
- (id)propertyForKey:(NSString *)key forService:(id)service {
  id v = self.properties[key];
  return v;
}
- (BOOL)setProperty:(id)value forKey:(NSString *)key forService:(id)service {
  return YES;
}
- (id)copyEventMatching:(NSDictionary *)matching forService:(id)service { return nil; }
- (BOOL)setOutputEvent:(id)event forService:(id)service { return YES; }
- (void)notification:(uint32_t)type withProperty:(NSDictionary *)prop forService:(id)service {
}
@end

int main(int argc, char **argv) {
  @autoreleasepool {
    // Read-only queries — the hinge angle (CoreMotion) — are answered before
    // any HID framework is loaded or an input service registered, so they
    // leave the guest untouched.
    if (argc == 2 && strcmp(argv[1], "read-angle") == 0) return printHingeAngles(NO);
    if (argc == 2 && strcmp(argv[1], "watch-angle") == 0) return printHingeAngles(YES);
    int first = 1;
    double deadline = INFINITY;
    if (argc > 1 && strcmp(argv[1], "--deadline") == 0) {
      if (argc < 3 || !parseHingeDeadline(argv[2], &deadline)) {
        fprintf(stderr, "--deadline requires a Unix time in seconds\n");
        return 2;
      }
      first = 3;
    }
    if (argc - first < 1) { fprintf(stderr, "usage: HingeControl read-angle | watch-angle | [--deadline <unix-seconds>] angle <deg> | sweep <from> <to> <ms> | orientation <portrait|pud|landscape-left|landscape-right> | serve\n"); return 2; }
    if (strcmp(argv[first], "orientation") == 0 &&
        (argc - first != 2 || !isHingeOrientation(@(argv[first + 1])))) {
      fprintf(stderr, "orientation requires portrait, pud, landscape-left or landscape-right\n");
      return 2;
    }
    if (hingeDeadlinePassed(deadline)) {
      fprintf(stderr, "deadline passed; nothing done\n");
      return HingeDeadlinePassedStatus;
    }
    printf("pid %d\n", getpid());
    fflush(stdout);
    dlopen("/System/Library/PrivateFrameworks/HID.framework/HID", RTLD_NOW);
    void *iokit = dlopen("/System/Library/Frameworks/IOKit.framework/IOKit", RTLD_NOW);
    IOHIDEventCreateVendorDefinedEvent = dlsym(iokit, "IOHIDEventCreateVendorDefinedEvent");
    if (!IOHIDEventCreateVendorDefinedEvent) { fprintf(stderr, "no IOHIDEventCreateVendorDefinedEvent\n"); return 1; }
    Class S = NSClassFromString(@"HIDVirtualEventService");
    dispatch_queue_t q = dispatch_queue_create("HingeControl", DISPATCH_QUEUE_SERIAL);
    // A virtual service of the given usage, activated; nil if it did not.
    id (^serviceOf)(unsigned, unsigned, NSString *, BOOL) = ^id(unsigned page, unsigned usage, NSString *product, BOOL builtIn) {
      id service = [[S alloc] init];
      ServiceDelegate *delegate = [ServiceDelegate new];
      NSMutableDictionary *props = [@{
        @"PrimaryUsagePage": @(page), @"PrimaryUsage": @(usage),
        @"DeviceUsagePairs": @[@{@"DeviceUsagePage": @(page), @"DeviceUsage": @(usage)}],
        @"Transport": @"CoreDevice", @"Product": product,
        @"VendorID": @0, @"ProductID": @0, @"VersionNumber": @0, @"ReportInterval": @8000,
      } mutableCopy];
      if (builtIn) props[@"Built-In"] = @YES;
      delegate.properties = props;
      objc_setAssociatedObject(service, "delegate", delegate, OBJC_ASSOCIATION_RETAIN_NONATOMIC);
      ((void (*)(id, SEL, id))objc_msgSend)(service, sel_registerName("setDelegate:"), delegate);
      ((void (*)(id, SEL, id))objc_msgSend)(service, sel_registerName("setDispatchQueue:"), q);
      ((void (*)(id, SEL))objc_msgSend)(service, sel_registerName("activate"));
      uint64_t sid = ((uint64_t (*)(id, SEL))objc_msgSend)(service, sel_registerName("serviceID"));
      return sid ? service : nil;
    };
    id service = serviceOf(0xFF61, 0x5B, @"quern hinge-control", NO);
    if (!service) { fprintf(stderr, "HID service did not activate\n"); return 1; }
    usleep(300 * 1000);   // let the event system enumerate them
    if (hingeDeadlinePassed(deadline)) {
      ((void (*)(id, SEL))objc_msgSend)(service, sel_registerName("cancel"));
      fprintf(stderr, "deadline passed before the helper was ready; nothing done\n");
      return HingeDeadlinePassedStatus;
    }

    BOOL (*dispatch)(id, SEL, id) = (BOOL (*)(id, SEL, id))objc_msgSend;
    BOOL (^send)(NSData *) = ^BOOL(NSData *payload) {
      IOHIDEventRef ev = IOHIDEventCreateVendorDefinedEvent(kCFAllocatorDefault, mach_absolute_time(), 0xFF61, 0x5B, 0,
        (uint8_t *)payload.bytes, payload.length, 0);
      BOOL ok = dispatch(service, sel_registerName("dispatchEvent:"), (__bridge id)ev);
      CFRelease(ev);
      return ok;
    };
    // One command: `angle D`, `sweep F T MS` or `orientation NAME`.
    // Exit status: 0 success, 1 dispatch failure, 2 invalid arguments.
    int (^perform)(NSArray<NSString *> *) = ^int(NSArray<NSString *> *words) {
      NSString *verb = words.firstObject ?: @"";
      if ([verb isEqualToString:@"angle"] && words.count >= 2) {
        return send(hingePayload(words[1].doubleValue)) ? 0 : 1;
      } else if ([verb isEqualToString:@"sweep"] && words.count >= 4) {
        double from = words[1].doubleValue, to = words[2].doubleValue, ms = words[3].doubleValue;
        int frames = (int)(ms / 16.667); if (frames < 1) frames = 1;
        for (int i = 1; i <= frames; i++) {
          double p = (double)i / frames, e = 1 - pow(1 - p, 3);
          if (!send(hingePayload(from + (to - from) * e))) return 1;
          usleep(16667);
        }
      } else if ([verb isEqualToString:@"orientation"] && words.count == 2) {
        return dispatchHingeOrientation(words[1], ^BOOL(const char *value) {
          return send(orientationPayload(value));
        });
      } else {
        return 2;
      }
      return 0;
    };
    NSMutableArray<NSString *> *words = [NSMutableArray array];
    for (int i = first; i < argc; i++) [words addObject:[NSString stringWithUTF8String:argv[i]]];
    int status = 0;
    if ([words.firstObject isEqualToString:@"serve"]) {
      // Commands line by line until stdin closes — the owner's exit.
      serveHingeCommands(stdin, stdout, perform);
    } else {
      status = perform(words);
      if (status) fprintf(stderr, "%s\n", status == 2 ? "bad arguments" : "dispatch failed");
    }
    usleep(300 * 1000);
    ((void (*)(id, SEL))objc_msgSend)(service, sel_registerName("cancel"));
    usleep(100 * 1000);
    return status;
  }
  return 0;
}

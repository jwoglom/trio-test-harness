// THVersionMock: loaded into the app under test by `th versionmock` (harness/versionmock.py), never shipped in it.
//
// What it does, all from one JSON file, so a test can feed the app any version-check answer and make it believe it
// is any build:
// - answers HTTP(S) requests whose URL a route names with the route's status, headers and body, or a network error
//   (an NSURLProtocol: URLSession.shared and sessions made from the default/ephemeral configurations);
// - overrides keys of the main bundle's Info.plist (CFBundleShortVersionString, CFBundleVersion, ...);
// - overrides keys of a property-list resource of the main bundle (Trio's BuildDetails.plist: com-trio-branch,
//   com-trio-commit-sha, com-trio-build-date, ...), by handing the app a merged copy;
// - removes UserDefaults keys once per `reset_token` (or at every launch), so cached answers and "shown in the last
//   24 hours" timestamps do not hide what the test is looking for.
//
// It is inert without its config file: $TH_VERSIONMOCK_CONFIG, else <app home>/Library/th-versionmock/config.json.
// Every request it answers and what it changed at launch go to log.jsonl next to the config, one JSON object per
// line, and to NSLog with the prefix [th-versionmock].
//
// Loading: on the simulator, simctl launch with SIMCTL_CHILD_DYLD_INSERT_LIBRARIES; on the jailbroken phone, as a
// MobileSubstrate/ElleKit library filtered to the app's bundle id.
//
// Config:
// {
//   "info": {"CFBundleShortVersionString": "0.6.0.12"},                  null removes a key
//   "plists": {"BuildDetails.plist": {"com-trio-branch": "dev", "com-trio-commit-sha": "a1b2c3d"}},
//   "routes": [{"url": "https://raw.githubusercontent.com/.../blacklisted-versions.json",   a trailing * = prefix
//               "status": 200, "headers": {}, "body": "...", "body_file": "bodies/x.json",  relative to the config
//               "error": "offline" | "timeout" | "cannot-find-host",                        instead of a response
//               "delay": 0.0}],                                                             seconds
//   "reset_defaults": ["latestVersionChecked", ...], "reset_token": "...", "reset_every_launch": false
// }

#import <Foundation/Foundation.h>
#import <objc/runtime.h>

static NSString *const THPrefix = @"[th-versionmock]";
static NSString *const THHandledKey = @"THVersionMockHandled";
static NSString *const THResetTokenKey = @"th-versionmock.reset-token";

static NSString *gConfigPath;
static NSString *gConfigDir;
static NSBundle *gMainBundle;
static NSDictionary *gInfoOverrides;
static NSDictionary<NSString *, NSString *> *gResourcePaths;   // "BuildDetails.plist" -> merged copy
static NSArray<NSDictionary *> *gRoutes;
static NSDate *gRoutesMTime;
static NSLock *gLock;

#pragma mark - config and log

static NSDictionary *THReadConfig(void) {
    NSData *data = [NSData dataWithContentsOfFile:gConfigPath];
    if (!data) {
        return nil;
    }
    id obj = [NSJSONSerialization JSONObjectWithData:data options:0 error:NULL];
    return [obj isKindOfClass:[NSDictionary class]] ? obj : nil;
}

static void THLogEvent(NSDictionary *event) {
    NSMutableDictionary *rec = [event mutableCopy];
    rec[@"t"] = [[NSISO8601DateFormatter new] stringFromDate:[NSDate date]];
    NSData *json = [NSJSONSerialization dataWithJSONObject:rec options:0 error:NULL];
    if (!json) {
        return;
    }
    NSString *line = [[NSString alloc] initWithData:json encoding:NSUTF8StringEncoding];
    NSLog(@"%@ %@", THPrefix, line);
    NSString *path = [gConfigDir stringByAppendingPathComponent:@"log.jsonl"];
    [gLock lock];
    NSFileHandle *fh = [NSFileHandle fileHandleForWritingAtPath:path];
    if (!fh) {
        [[NSFileManager defaultManager] createFileAtPath:path contents:nil attributes:nil];
        fh = [NSFileHandle fileHandleForWritingAtPath:path];
    }
    [fh seekToEndOfFile];
    [fh writeData:[[line stringByAppendingString:@"\n"] dataUsingEncoding:NSUTF8StringEncoding]];
    [fh closeFile];
    [gLock unlock];
}

// The routes, read again whenever the config file changes, so a test can change an answer without a relaunch.
static NSArray<NSDictionary *> *THRoutes(void) {
    NSDate *mtime = [[[NSFileManager defaultManager] attributesOfItemAtPath:gConfigPath error:NULL] fileModificationDate];
    [gLock lock];
    if (mtime && ![mtime isEqualToDate:gRoutesMTime]) {
        NSArray *routes = THReadConfig()[@"routes"];
        gRoutes = [routes isKindOfClass:[NSArray class]] ? routes : @[];
        gRoutesMTime = mtime;
    }
    NSArray *routes = gRoutes ?: @[];
    [gLock unlock];
    return routes;
}

static NSDictionary *THRouteFor(NSURL *url) {
    NSString *s = url.absoluteString;
    if (!s) {
        return nil;
    }
    for (NSDictionary *route in THRoutes()) {
        if (![route isKindOfClass:[NSDictionary class]]) {
            continue;
        }
        NSString *u = route[@"url"];
        if (![u isKindOfClass:[NSString class]] || u.length == 0) {
            continue;
        }
        BOOL hit = [u hasSuffix:@"*"] ? [s hasPrefix:[u substringToIndex:u.length - 1]] : [s isEqualToString:u];
        if (hit) {
            return route;
        }
    }
    return nil;
}

static void THApply(NSMutableDictionary *target, NSDictionary *overrides) {
    [overrides enumerateKeysAndObjectsUsingBlock:^(id key, id value, BOOL *stop) {
        if (value == [NSNull null]) {
            [target removeObjectForKey:key];
        } else {
            target[key] = value;
        }
    }];
}

#pragma mark - network

@interface THVersionMockProtocol : NSURLProtocol
@property (atomic) BOOL stopped;
@end

@implementation THVersionMockProtocol

+ (BOOL)canInitWithRequest:(NSURLRequest *)request {
    if ([NSURLProtocol propertyForKey:THHandledKey inRequest:request]) {
        return NO;
    }
    return THRouteFor(request.URL) != nil;
}

+ (BOOL)canInitWithTask:(NSURLSessionTask *)task {
    return task.currentRequest && [self canInitWithRequest:task.currentRequest];
}

+ (NSURLRequest *)canonicalRequestForRequest:(NSURLRequest *)request {
    return request;
}

- (void)respondWithRoute:(NSDictionary *)route {
    if (self.stopped) {
        return;
    }
    NSURL *url = self.request.URL;
    id<NSURLProtocolClient> client = self.client;
    NSString *error = [route[@"error"] isKindOfClass:[NSString class]] ? route[@"error"] : nil;
    if (error.length) {
        NSInteger code = NSURLErrorNotConnectedToInternet;
        if ([error isEqualToString:@"timeout"]) {
            code = NSURLErrorTimedOut;
        } else if ([error isEqualToString:@"cannot-find-host"]) {
            code = NSURLErrorCannotFindHost;
        }
        THLogEvent(@{@"event": @"request", @"url": url.absoluteString ?: @"", @"error": error});
        [client URLProtocol:self
           didFailWithError:[NSError errorWithDomain:NSURLErrorDomain
                                                code:code
                                            userInfo:@{NSURLErrorFailingURLErrorKey: url,
                                                       NSURLErrorFailingURLStringErrorKey: url.absoluteString ?: @""}]];
        return;
    }

    NSData *body = [NSData data];
    NSString *bodyFile = [route[@"body_file"] isKindOfClass:[NSString class]] ? route[@"body_file"] : nil;
    if (bodyFile.length) {
        NSString *path = [bodyFile isAbsolutePath] ? bodyFile : [gConfigDir stringByAppendingPathComponent:bodyFile];
        body = [NSData dataWithContentsOfFile:path] ?: [NSData data];
    } else if ([route[@"body"] isKindOfClass:[NSString class]]) {
        body = [route[@"body"] dataUsingEncoding:NSUTF8StringEncoding];
    }
    NSInteger status = [route[@"status"] respondsToSelector:@selector(integerValue)] ? [route[@"status"] integerValue] : 200;
    NSMutableDictionary *headers = [@{@"Content-Type": @"text/plain; charset=utf-8",
                                      @"Content-Length": [NSString stringWithFormat:@"%lu", (unsigned long)body.length],
                                      @"X-TH-VersionMock": @"1"} mutableCopy];
    if ([route[@"headers"] isKindOfClass:[NSDictionary class]]) {
        [headers addEntriesFromDictionary:route[@"headers"]];
    }
    NSHTTPURLResponse *response = [[NSHTTPURLResponse alloc] initWithURL:url
                                                              statusCode:status
                                                             HTTPVersion:@"HTTP/1.1"
                                                            headerFields:headers];
    THLogEvent(@{@"event": @"request", @"url": url.absoluteString ?: @"", @"status": @(status), @"bytes": @(body.length)});
    [client URLProtocol:self didReceiveResponse:response cacheStoragePolicy:NSURLCacheStorageNotAllowed];
    [client URLProtocol:self didLoadData:body];
    [client URLProtocolDidFinishLoading:self];
}

- (void)startLoading {
    NSDictionary *route = THRouteFor(self.request.URL) ?: @{@"status": @404};
    double delay = [route[@"delay"] respondsToSelector:@selector(doubleValue)] ? [route[@"delay"] doubleValue] : 0;
    if (delay <= 0) {
        [self respondWithRoute:route];
        return;
    }
    // The client must hear back on the thread that called startLoading: run the answer on its run loop.
    CFRunLoopRef runLoop = CFRunLoopGetCurrent();
    CFRetain(runLoop);
    __weak THVersionMockProtocol *weakSelf = self;
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(delay * NSEC_PER_SEC)),
                   dispatch_get_global_queue(QOS_CLASS_DEFAULT, 0), ^{
        CFRunLoopPerformBlock(runLoop, kCFRunLoopCommonModes, ^{
            [weakSelf respondWithRoute:route];
        });
        CFRunLoopWakeUp(runLoop);
        CFRelease(runLoop);
    });
}

- (void)stopLoading {
    self.stopped = YES;
}

@end

#pragma mark - swizzling

static IMP THSwap(Class cls, SEL sel, IMP replacement) {
    Method m = class_getInstanceMethod(cls, sel);
    return m ? method_setImplementation(m, replacement) : NULL;
}

static IMP gOrigInfoDictionary;
static NSDictionary *THInfoDictionary(NSBundle *self, SEL _cmd) {
    NSDictionary *orig = ((NSDictionary * (*)(id, SEL))gOrigInfoDictionary)(self, _cmd);
    if (self != gMainBundle || !orig) {
        return orig;
    }
    static NSDictionary *merged;
    static dispatch_once_t once;
    dispatch_once(&once, ^{
        NSMutableDictionary *m = [orig mutableCopy];
        THApply(m, gInfoOverrides);
        merged = [m copy];
    });
    return merged;
}

static IMP gOrigObjectForInfoKey;
static id THObjectForInfoDictionaryKey(NSBundle *self, SEL _cmd, NSString *key) {
    if (self == gMainBundle && key) {
        id value = gInfoOverrides[key];
        if (value) {
            return value == [NSNull null] ? nil : value;
        }
    }
    return ((id (*)(id, SEL, NSString *))gOrigObjectForInfoKey)(self, _cmd, key);
}

static NSString *THResourceOverride(NSBundle *bundle, NSString *name, NSString *ext) {
    if (bundle != gMainBundle || !name) {
        return nil;
    }
    NSString *key = ext.length ? [name stringByAppendingPathExtension:ext] : name;
    return gResourcePaths[key];
}

static IMP gOrigURLForResource;
static NSURL *THURLForResource(NSBundle *self, SEL _cmd, NSString *name, NSString *ext) {
    NSString *path = THResourceOverride(self, name, ext);
    if (path) {
        return [NSURL fileURLWithPath:path];
    }
    return ((NSURL * (*)(id, SEL, NSString *, NSString *))gOrigURLForResource)(self, _cmd, name, ext);
}

static IMP gOrigPathForResource;
static NSString *THPathForResource(NSBundle *self, SEL _cmd, NSString *name, NSString *ext) {
    NSString *path = THResourceOverride(self, name, ext);
    if (path) {
        return path;
    }
    return ((NSString * (*)(id, SEL, NSString *, NSString *))gOrigPathForResource)(self, _cmd, name, ext);
}

// Sessions made from a configuration ask its protocolClasses, not the global registry: put ours first.
static IMP gOrigDefaultConfig;
static IMP gOrigEphemeralConfig;
static NSURLSessionConfiguration *THWithProtocol(NSURLSessionConfiguration *config) {
    NSMutableArray *classes = [config.protocolClasses mutableCopy] ?: [NSMutableArray array];
    if (![classes containsObject:[THVersionMockProtocol class]]) {
        [classes insertObject:[THVersionMockProtocol class] atIndex:0];
    }
    config.protocolClasses = classes;
    return config;
}
static NSURLSessionConfiguration *THDefaultConfig(id self, SEL _cmd) {
    return THWithProtocol(((NSURLSessionConfiguration * (*)(id, SEL))gOrigDefaultConfig)(self, _cmd));
}
static NSURLSessionConfiguration *THEphemeralConfig(id self, SEL _cmd) {
    return THWithProtocol(((NSURLSessionConfiguration * (*)(id, SEL))gOrigEphemeralConfig)(self, _cmd));
}

#pragma mark - setup

// Writes a copy of each named plist resource with the overrides applied, into <config dir>/resources/.
static NSDictionary<NSString *, NSString *> *THMergedResources(NSDictionary *plists, NSMutableArray *notes) {
    NSMutableDictionary *paths = [NSMutableDictionary dictionary];
    NSString *dir = [gConfigDir stringByAppendingPathComponent:@"resources"];
    [[NSFileManager defaultManager] createDirectoryAtPath:dir withIntermediateDirectories:YES attributes:nil error:NULL];
    [plists enumerateKeysAndObjectsUsingBlock:^(id name, id overrides, BOOL *stop) {
        if (![name isKindOfClass:[NSString class]] || ![overrides isKindOfClass:[NSDictionary class]]) {
            return;
        }
        NSString *original = [gMainBundle pathForResource:[name stringByDeletingPathExtension] ofType:[name pathExtension]];
        NSMutableDictionary *merged = [NSMutableDictionary dictionary];
        if (original) {
            NSData *data = [NSData dataWithContentsOfFile:original];
            id plist = data ? [NSPropertyListSerialization propertyListWithData:data
                                                                        options:NSPropertyListMutableContainers
                                                                         format:NULL
                                                                          error:NULL] : nil;
            if ([plist isKindOfClass:[NSDictionary class]]) {
                merged = plist;
            }
        }
        THApply(merged, overrides);
        NSData *out = [NSPropertyListSerialization dataWithPropertyList:merged
                                                                 format:NSPropertyListXMLFormat_v1_0
                                                                options:0
                                                                  error:NULL];
        NSString *path = [dir stringByAppendingPathComponent:name];
        if (out && [out writeToFile:path atomically:YES]) {
            paths[name] = path;
            [notes addObject:[NSString stringWithFormat:@"%@%@", name, original ? @"" : @" (not in the bundle: new)"]];
        }
    }];
    return paths;
}

static void THResetDefaults(NSDictionary *cfg) {
    NSArray *keys = [cfg[@"reset_defaults"] isKindOfClass:[NSArray class]] ? cfg[@"reset_defaults"] : @[];
    if (keys.count == 0) {
        return;
    }
    NSUserDefaults *defaults = [NSUserDefaults standardUserDefaults];
    NSString *token = [cfg[@"reset_token"] isKindOfClass:[NSString class]] ? cfg[@"reset_token"] : @"";
    BOOL every = [cfg[@"reset_every_launch"] respondsToSelector:@selector(boolValue)] && [cfg[@"reset_every_launch"] boolValue];
    if (!every && [token isEqualToString:[defaults stringForKey:THResetTokenKey] ?: @"<none>"]) {
        return;
    }
    NSMutableArray *removed = [NSMutableArray array];
    for (NSString *key in keys) {
        if ([key isKindOfClass:[NSString class]] && [defaults objectForKey:key] != nil) {
            [defaults removeObjectForKey:key];
            [removed addObject:key];
        }
    }
    [defaults setObject:token forKey:THResetTokenKey];
    THLogEvent(@{@"event": @"reset-defaults", @"removed": removed, @"token": token});
}

__attribute__((constructor)) static void THVersionMockInit(void) {
    @autoreleasepool {
        const char *env = getenv("TH_VERSIONMOCK_CONFIG");
        gConfigPath = env && *env ? [NSString stringWithUTF8String:env]
                                  : [NSHomeDirectory() stringByAppendingPathComponent:@"Library/th-versionmock/config.json"];
        gConfigDir = [gConfigPath stringByDeletingLastPathComponent];
        gLock = [NSLock new];
        NSDictionary *cfg = THReadConfig();
        if (!cfg) {
            NSLog(@"%@ no config at %@: inactive", THPrefix, gConfigPath);
            return;
        }
        gMainBundle = [NSBundle mainBundle];
        gInfoOverrides = [cfg[@"info"] isKindOfClass:[NSDictionary class]] ? cfg[@"info"] : @{};
        NSMutableArray *resourceNotes = [NSMutableArray array];
        NSDictionary *plists = [cfg[@"plists"] isKindOfClass:[NSDictionary class]] ? cfg[@"plists"] : @{};
        gResourcePaths = THMergedResources(plists, resourceNotes);   // before the swizzles: reads the real files

        gOrigInfoDictionary = THSwap([NSBundle class], @selector(infoDictionary), (IMP)THInfoDictionary);
        gOrigObjectForInfoKey = THSwap([NSBundle class], @selector(objectForInfoDictionaryKey:), (IMP)THObjectForInfoDictionaryKey);
        gOrigURLForResource = THSwap([NSBundle class], @selector(URLForResource:withExtension:), (IMP)THURLForResource);
        gOrigPathForResource = THSwap([NSBundle class], @selector(pathForResource:ofType:), (IMP)THPathForResource);

        Class meta = object_getClass([NSURLSessionConfiguration class]);
        gOrigDefaultConfig = THSwap(meta, @selector(defaultSessionConfiguration), (IMP)THDefaultConfig);
        gOrigEphemeralConfig = THSwap(meta, @selector(ephemeralSessionConfiguration), (IMP)THEphemeralConfig);
        [NSURLProtocol registerClass:[THVersionMockProtocol class]];

        NSArray *routes = THRoutes();
        NSMutableArray *urls = [NSMutableArray array];
        for (NSDictionary *r in routes) {
            if ([r isKindOfClass:[NSDictionary class]] && [r[@"url"] isKindOfClass:[NSString class]]) {
                [urls addObject:r[@"url"]];
            }
        }
        THLogEvent(@{@"event": @"loaded",
                     @"bundle": gMainBundle.bundleIdentifier ?: @"",
                     @"info": gInfoOverrides,
                     @"plists": plists,
                     @"resources": resourceNotes,
                     @"routes": urls});
        THResetDefaults(cfg);
    }
}

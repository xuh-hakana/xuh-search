// search · 原生悬浮窗外壳（Objective-C + WKWebView）
// 编译：clang -fobjc-arc -framework Cocoa -framework WebKit -framework Carbon launcher.m -o search
//
// 这个文件只干四件事：
// 1. 起本地 Python 服务（只监听 127.0.0.1，随机端口）；
// 2. 等它健康检查通过，把页面装进一个无边框、永远在最上层的面板；
// 3. 给网页一个「原生通道」——拖动窗口、置顶、收起、打开外部链接；
// 4. 菜单栏图标 + 全局快捷键 ⌃⌥S，随时唤出/收起。

#import <Cocoa/Cocoa.h>
#import <WebKit/WebKit.h>
#import <Carbon/Carbon.h>
#import <arpa/inet.h>
#import <netinet/in.h>
#import <sys/socket.h>
#import <unistd.h>

static NSTask *gServer = nil;
static NSString *gLogPath = nil;
static const OSType kHotKeySignature = 'XHSR';
static const UInt32 kHotKeyId = 1;

static OSStatus hotKeyHandler(EventHandlerCallRef nextHandler, EventRef event, void *userData);

static int FreeLoopbackPort(void) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) return 0;
    int yes = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    addr.sin_port = 0;
    int port = 0;
    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) == 0) {
        socklen_t len = sizeof(addr);
        if (getsockname(fd, (struct sockaddr *)&addr, &len) == 0) port = ntohs(addr.sin_port);
    }
    close(fd);
    return port;
}

static NSString *FindPython(void) {
    NSArray *candidates = @[
        @"/opt/homebrew/bin/python3",
        @"/usr/local/bin/python3",
        @"/Library/Frameworks/Python.framework/Versions/Current/bin/python3",
        @"/usr/bin/python3",
    ];
    NSFileManager *fm = [NSFileManager defaultManager];
    for (NSString *path in candidates) {
        if ([fm isExecutableFileAtPath:path]) return path;
    }
    return nil;
}

static NSString *FindProjectRoot(NSString **outServerPath) {
    NSString *bundle = [[NSBundle mainBundle] bundlePath];
    NSArray *roots = @[
        [bundle stringByDeletingLastPathComponent],
        [bundle stringByAppendingPathComponent:@"Contents/Resources"],
        [[NSBundle mainBundle] resourcePath] ?: @"",
    ];
    NSFileManager *fm = [NSFileManager defaultManager];
    for (NSString *root in roots) {
        NSString *server = [root stringByAppendingPathComponent:@"server/server.py"];
        if ([fm fileExistsAtPath:server]) {
            if (outServerPath) *outServerPath = server;
            return root;
        }
    }
    return nil;
}

static void AppendLog(NSString *text) {
    // 早期（还没起服务时）也要能记日志，否则划词这类启动期信息全丢了
    if (!gLogPath) {
        gLogPath = [NSHomeDirectory() stringByAppendingPathComponent:@"Library/Logs/XuhuanSearch.log"];
    }
    if (!gLogPath) return;
    NSFileManager *fm = [NSFileManager defaultManager];
    if (![fm fileExistsAtPath:gLogPath]) [fm createFileAtPath:gLogPath contents:[NSData data] attributes:nil];
    NSFileHandle *handle = [NSFileHandle fileHandleForWritingAtPath:gLogPath];
    if (!handle) return;
    [handle seekToEndOfFile];
    [handle writeData:[[text stringByAppendingString:@"\n"] dataUsingEncoding:NSUTF8StringEncoding]];
    [handle closeFile];
}

@interface SearchPanel : NSPanel
@end

@implementation SearchPanel
// 无边框面板默认不接受键盘焦点；这里让它能打字，但不抢别的 App 的前台。
- (BOOL)canBecomeKeyWindow { return YES; }
- (BOOL)canBecomeMainWindow { return NO; }
@end

#pragma mark - 网页视图
// 面板不是 key window 的时候，系统会把「第一次点击」吃掉、只用来激活窗口，
// 网页那边收不到 mousedown → 表现就是「要点一下才能拖」。这里让首次点击直接进网页。
@interface SearchWebView : WKWebView
@end

@implementation SearchWebView
- (BOOL)acceptsFirstMouse:(NSEvent *)event { return YES; }
- (BOOL)acceptsFirstResponder { return YES; }
@end

#pragma mark - 划词小圆点
// 在任何程序里选中文字，鼠标旁边冒出一个白圆；鼠标一碰上去才查，
// 不碰就不打扰。用系统辅助功能接口读选区，跟 Easydict 一个路子。

static NSString *SelectedText(void) {
    if (!AXIsProcessTrusted()) return nil;
    AXUIElementRef sys = AXUIElementCreateSystemWide();
    if (!sys) return nil;
    NSString *text = nil;
    CFTypeRef focused = NULL;
    if (AXUIElementCopyAttributeValue(sys, kAXFocusedUIElementAttribute, &focused) == kAXErrorSuccess && focused) {
        CFTypeRef sel = NULL;
        if (AXUIElementCopyAttributeValue((AXUIElementRef)focused, kAXSelectedTextAttribute, &sel) == kAXErrorSuccess && sel) {
            if (CFGetTypeID(sel) == CFStringGetTypeID()) text = [(__bridge NSString *)sel copy];
            CFRelease(sel);
        }
        CFRelease(focused);
    }
    CFRelease(sys);
    return text;
}

@interface DotView : NSView
@property (strong) NSTrackingArea *track;
@property (copy) void (^onEnter)(void);
@property (copy) void (^onExit)(void);
@end

@implementation DotView

- (BOOL)acceptsFirstMouse:(NSEvent *)event { return YES; }

- (void)updateTrackingAreas {
    [super updateTrackingAreas];
    if (self.track) [self removeTrackingArea:self.track];
    NSTrackingAreaOptions opts = NSTrackingMouseEnteredAndExited | NSTrackingActiveAlways | NSTrackingInVisibleRect;
    self.track = [[NSTrackingArea alloc] initWithRect:self.bounds options:opts owner:self userInfo:nil];
    [self addTrackingArea:self.track];
}

- (void)mouseEntered:(NSEvent *)event { if (self.onEnter) self.onEnter(); }
- (void)mouseExited:(NSEvent *)event { if (self.onExit) self.onExit(); }

- (void)drawRect:(NSRect)dirtyRect {
    NSRect b = NSInsetRect(self.bounds, 2.5, 2.5);
    NSBezierPath *circle = [NSBezierPath bezierPathWithOvalInRect:b];
    [[NSColor whiteColor] setFill];
    [circle fill];
    [[NSColor colorWithWhite:0 alpha:0.14] setStroke];
    circle.lineWidth = 0.5;
    [circle stroke];

    NSPoint c = NSMakePoint(NSMidX(b) - 0.8, NSMidY(b) + 0.8);
    NSBezierPath *lens = [NSBezierPath bezierPathWithOvalInRect:NSMakeRect(c.x - 4.6, c.y - 4.6, 8.2, 8.2)];
    lens.lineWidth = 1.7;
    [[NSColor systemBlueColor] setStroke];
    [lens stroke];

    NSBezierPath *handle = [NSBezierPath bezierPath];
    [handle moveToPoint:NSMakePoint(c.x + 2.4, c.y - 2.4)];
    [handle lineToPoint:NSMakePoint(c.x + 5.2, c.y - 5.2)];
    handle.lineWidth = 1.9;
    handle.lineCapStyle = NSLineCapStyleRound;
    [handle stroke];
}
@end

@interface SelectionWatcher : NSObject
@property (strong) NSPanel *dot;
@property (strong) DotView *dotView;
@property (strong) NSTimer *hideTimer;
@property (strong) id monitor;
@property (copy) NSString *pendingText;
@property (copy) void (^onPick)(NSString *text, NSPoint dotOrigin);
@property (assign) BOOL on;
+ (instancetype)shared;
- (void)start;
- (void)stop;
@end

@implementation SelectionWatcher

+ (instancetype)shared {
    static SelectionWatcher *single = nil;
    static dispatch_once_t once;
    dispatch_once(&once, ^{ single = [[SelectionWatcher alloc] init]; });
    return single;
}

- (instancetype)init {
    if ((self = [super init])) [self buildDot];
    return self;
}

- (void)buildDot {
    CGFloat side = 30;
    self.dot = [[NSPanel alloc] initWithContentRect:NSMakeRect(0, 0, side, side)
                                          styleMask:NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
                                            backing:NSBackingStoreBuffered
                                              defer:NO];
    self.dot.level = NSFloatingWindowLevel + 2;
    self.dot.opaque = NO;
    self.dot.backgroundColor = [NSColor clearColor];
    self.dot.hasShadow = YES;
    self.dot.ignoresMouseEvents = NO;
    self.dot.hidesOnDeactivate = NO;
    self.dot.collectionBehavior = NSWindowCollectionBehaviorCanJoinAllSpaces |
                                  NSWindowCollectionBehaviorFullScreenAuxiliary |
                                  NSWindowCollectionBehaviorStationary;
    DotView *view = [[DotView alloc] initWithFrame:NSMakeRect(0, 0, side, side)];
    __weak SelectionWatcher *weakSelf = self;
    view.onEnter = ^{ [weakSelf dotHovered]; };
    view.onExit = ^{ [weakSelf scheduleHide:0.7]; };
    self.dotView = view;
    self.dot.contentView = view;
}

- (void)start {
    if (self.on) return;
    self.on = YES;
    __weak SelectionWatcher *weakSelf = self;
    self.monitor = [NSEvent addGlobalMonitorForEventsMatchingMask:NSEventMaskLeftMouseUp
                                                          handler:^(NSEvent *event) {
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(0.13 * NSEC_PER_SEC)),
                       dispatch_get_main_queue(), ^{ [weakSelf probe]; });
    }];
    AppendLog([NSString stringWithFormat:@"[select] 划词监听已开，辅助功能权限=%d", (int)AXIsProcessTrusted()]);
}

- (void)stop {
    if (!self.on) return;
    self.on = NO;
    [self hideNow];
    if (self.monitor) { [NSEvent removeMonitor:self.monitor]; self.monitor = nil; }
    AppendLog(@"[select] 划词监听已关");
}

- (void)probe {
    NSString *text = SelectedText();
    if (!text) { [self hideNow]; return; }
    text = [text stringByTrimmingCharactersInSet:[NSCharacterSet whitespaceAndNewlineCharacterSet]];
    if (text.length < 1 || text.length > 60) { [self hideNow]; return; }
    if (![self hasRealWord:text]) { [self hideNow]; return; }
    self.pendingText = text;
    [self showDotAt:[NSEvent mouseLocation]];
}

// 全是标点或空白就别冒头了
- (BOOL)hasRealWord:(NSString *)text {
    NSCharacterSet *alnum = [NSCharacterSet alphanumericCharacterSet];
    for (NSUInteger i = 0; i < text.length; i++) {
        unichar c = [text characterAtIndex:i];
        if (c > 0x2E80 || [alnum characterIsMember:c]) return YES;
    }
    return NO;
}

- (void)showDotAt:(NSPoint)mouse {
    NSRect screen = [NSScreen mainScreen].visibleFrame;
    NSPoint origin = NSMakePoint(mouse.x + 14, mouse.y - 36);
    origin.x = MIN(MAX(origin.x, screen.origin.x + 4), NSMaxX(screen) - 34);
    origin.y = MIN(MAX(origin.y, screen.origin.y + 4), NSMaxY(screen) - 34);
    [self.dot setFrameOrigin:origin];
    [self.dot orderFrontRegardless];
    [self scheduleHide:2.2];
}

- (void)scheduleHide:(NSTimeInterval)delay {
    [self.hideTimer invalidate];
    self.hideTimer = [NSTimer scheduledTimerWithTimeInterval:delay target:self
                                                    selector:@selector(hideNow) userInfo:nil repeats:NO];
}

- (void)hideNow {
    [self.hideTimer invalidate];
    self.hideTimer = nil;
    [self.dot orderOut:nil];
}

- (void)dotHovered {
    [self.hideTimer invalidate];
    self.hideTimer = nil;
    NSString *text = self.pendingText;
    NSPoint at = self.dot.frame.origin;
    [self hideNow];
    if (text.length && self.onPick) self.onPick(text, at);
}

@end

@interface DragBridge : NSObject
@property (assign) NSPoint startMouse;
@property (assign) NSPoint startOrigin;
@property (strong) NSTimer *timer;
@end

@implementation DragBridge
- (void)beginWithWindow:(NSWindow *)window {
    self.startMouse = [NSEvent mouseLocation];
    self.startOrigin = window.frame.origin;
    if (self.timer) [self.timer invalidate];
    __weak NSWindow *weakWindow = window;
    self.timer = [NSTimer scheduledTimerWithTimeInterval:1.0 / 90.0 repeats:YES block:^(NSTimer *timer) {
        NSWindow *strongWindow = weakWindow;
        if (!strongWindow) { [timer invalidate]; return; }
        if (([NSEvent pressedMouseButtons] & 1) == 0) {
            [timer invalidate];
            return;
        }
        NSPoint current = [NSEvent mouseLocation];
        CGFloat dx = current.x - self.startMouse.x;
        CGFloat dy = current.y - self.startMouse.y;
        [strongWindow setFrameOrigin:NSMakePoint(self.startOrigin.x + dx, self.startOrigin.y + dy)];
    }];
}
@end

@interface SearchApp : NSObject <NSApplicationDelegate, NSWindowDelegate, WKScriptMessageHandler,
                                 WKNavigationDelegate>
@property (strong) SearchPanel *panel;
@property (strong) WKWebView *webView;
@property (strong) NSStatusItem *statusItem;
@property (strong) DragBridge *drag;
@property (assign) int port;
@property (assign) int tries;
@property (strong) NSTimer *pollTimer;
@property (copy) NSString *pageURL;
@property (assign) BOOL pinned;
@property (assign) BOOL docked;
@property (assign) NSRect dockSaved;
@property (strong) SelectionWatcher *selWatcher;
@end

@implementation SearchApp

#pragma mark - 启动
- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    self.pinned = YES;
    [self setupMenu];
    [self setupSelectionWatcher];   // 要比状态栏菜单早，菜单要读它的开关状态
    [self setupStatusItem];
    [self setupPanel];
    [self registerHotKey];
    [self showLoading:@"正在启动本地服务…"];
    if (![self startServer]) return;
    self.tries = 0;
    self.pollTimer = [NSTimer scheduledTimerWithTimeInterval:0.4 target:self
                                                    selector:@selector(pollHealth) userInfo:nil repeats:YES];
}

- (void)setupPanel {
    NSRect visible = [NSScreen mainScreen].visibleFrame;
    CGFloat width = 320, height = visible.size.height - 40;
    NSRect frame = NSMakeRect(visible.origin.x + visible.size.width - width - 24,
                              visible.origin.y + visible.size.height - height - 12,
                              width, height);
    self.panel = [[SearchPanel alloc] initWithContentRect:frame
                                                styleMask:NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
                                                  backing:NSBackingStoreBuffered
                                                    defer:NO];
    self.panel.level = NSFloatingWindowLevel;
    self.panel.opaque = NO;
    self.panel.backgroundColor = [NSColor clearColor];
    self.panel.hasShadow = YES;
    self.panel.movableByWindowBackground = NO;
    self.panel.becomesKeyOnlyIfNeeded = YES;
    self.panel.hidesOnDeactivate = NO;
    self.panel.releasedWhenClosed = NO;
    self.panel.collectionBehavior = NSWindowCollectionBehaviorCanJoinAllSpaces |
                                    NSWindowCollectionBehaviorFullScreenAuxiliary |
                                    NSWindowCollectionBehaviorStationary;
    self.panel.delegate = self;
    [self.panel setFrameAutosaveName:@"XuhuanSearchPanel"];

    NSView *host = self.panel.contentView;
    host.wantsLayer = YES;
    host.layer.cornerRadius = 14;
    host.layer.masksToBounds = YES;

    WKWebViewConfiguration *config = [[WKWebViewConfiguration alloc] init];
    config.websiteDataStore = [WKWebsiteDataStore defaultDataStore];
    WKUserContentController *controller = [[WKUserContentController alloc] init];
    [controller addScriptMessageHandler:self name:@"native"];
    config.userContentController = controller;
    WKUserScript *bootstrap = [[WKUserScript alloc]
        initWithSource:@"document.documentElement.classList.add('is-app');"
         injectionTime:WKUserScriptInjectionTimeAtDocumentStart
      forMainFrameOnly:YES];
    [config.userContentController addUserScript:bootstrap];

    self.webView = [[SearchWebView alloc] initWithFrame:host.bounds configuration:config];
    self.webView.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
    self.webView.wantsLayer = YES;
    self.webView.layer.cornerRadius = 14;
    self.webView.layer.masksToBounds = YES;
    @try { [self.webView setValue:@NO forKey:@"drawsBackground"]; } @catch (NSException *e) { }
    if (@available(macOS 12.0, *)) self.webView.underPageBackgroundColor = [NSColor clearColor];
    [host addSubview:self.webView];
    self.webView.navigationDelegate = self;
    [self.panel makeKeyAndOrderFront:nil];
}

#pragma mark - 划词触发
- (void)setupSelectionWatcher {
    self.selWatcher = [SelectionWatcher shared];
    __weak SearchApp *weakSelf = self;
    self.selWatcher.onPick = ^(NSString *text, NSPoint dotOrigin) {
        [weakSelf showPanelWithTerm:text nearPoint:dotOrigin];
    };
    // 没有辅助功能权限就读不到别的程序的选区，先按系统方式请求一次
    if (!AXIsProcessTrusted()) {
        NSDictionary *opts = @{(__bridge id)kAXTrustedCheckOptionPrompt: @YES};
        AXIsProcessTrustedWithOptions((__bridge CFDictionaryRef)opts);
    }
    BOOL wantDot = [[NSUserDefaults standardUserDefaults] objectForKey:@"SelectionDot"] ?
                   [[NSUserDefaults standardUserDefaults] boolForKey:@"SelectionDot"] : YES;
    if (wantDot) [self.selWatcher start];
}

- (void)toggleSelectionDot:(NSMenuItem *)item {
    BOOL next = !self.selWatcher.on;
    if (next) [self.selWatcher start]; else [self.selWatcher stop];
    [[NSUserDefaults standardUserDefaults] setBool:next forKey:@"SelectionDot"];
    item.state = next ? NSControlStateValueOn : NSControlStateValueOff;
}

// 把词塞进页面并查，窗口挪到鼠标附近（别盖住刚选的文字）
- (void)showPanelWithTerm:(NSString *)term nearPoint:(NSPoint)point {
    // 已经吸在命令方块角上就别乱跑，保持那块小窗的位置
    if (!self.docked) {
        NSRect screen = [NSScreen mainScreen].visibleFrame;
        NSRect f = self.panel.frame;
        CGFloat x = point.x - 4;
        CGFloat y = point.y - f.size.height - 10;
        if (y < screen.origin.y + 8) y = point.y + 46;
        x = MIN(MAX(x, screen.origin.x + 8), NSMaxX(screen) - f.size.width - 8);
        y = MIN(MAX(y, screen.origin.y + 8), NSMaxY(screen) - f.size.height - 8);
        [self.panel setFrame:NSMakeRect(x, y, f.size.width, f.size.height) display:YES];
    }
    [self.panel orderFrontRegardless];

    NSData *raw = [NSJSONSerialization dataWithJSONObject:@[term] options:0 error:nil];
    NSString *json = [[NSString alloc] initWithData:raw encoding:NSUTF8StringEncoding];
    NSString *quoted = json.length > 2 ? [json substringWithRange:NSMakeRange(1, json.length - 2)] : @"\"\"";
    NSString *js = [NSString stringWithFormat:@"window.__setTerm && window.__setTerm(%@)", quoted];
    [self.webView evaluateJavaScript:js completionHandler:nil];
    AppendLog([NSString stringWithFormat:@"[select] 命中「%@」→ 面板已打开", term]);
}

// 诊断：把页面实际排版宽度写进日志，方便定位「右侧被切」
- (void)webView:(WKWebView *)webView didFinishNavigation:(WKNavigation *)navigation {
    NSString *probe = @"(function(){"
        "var r=[];r.push('win='+window.innerWidth+'x'+window.innerHeight);"
        "r.push('doc='+document.documentElement.clientWidth+'/'+document.documentElement.scrollWidth);"
        "r.push('dpr='+window.devicePixelRatio);"
        "if(window.visualViewport)r.push('vv='+Math.round(window.visualViewport.width));"
        "['.panel','.topbar','.pills','.quick','.foot','.seg','.stat','.roots','.root-chips','.board'].forEach(function(s){"
        "var e=document.querySelector(s);if(!e)return;var b=e.getBoundingClientRect();"
        "r.push(s+'@'+Math.round(b.left)+'+'+Math.round(b.width));});"
        "return r.join(' | ');})()";
    __weak SearchApp *weakSelf = self;
    [webView evaluateJavaScript:probe completionHandler:^(id result, NSError *error) {
        AppendLog([NSString stringWithFormat:@"[layout] %@ %@", result ?: @"nil", error ? error.localizedDescription : @""]);
        (void)weakSelf;
    }];
    NSString *axJs = [NSString stringWithFormat:@"window.__setAX && window.__setAX(%@)",
                      AXIsProcessTrusted() ? @"true" : @"false"];
    [webView evaluateJavaScript:axJs completionHandler:nil];

}

#pragma mark - 服务
- (BOOL)startServer {
    NSString *serverPath = nil;
    NSString *root = FindProjectRoot(&serverPath);
    AppendLog([NSString stringWithFormat:@"[server] 项目根=%@ 服务=%@", root ?: @"没找到", serverPath ?: @"没找到"]);
    if (!root || !serverPath) {
        [self showError:@"找不到 server/server.py"
                 detail:@"请把 search.app 留在 search 文件夹里，不要单独拖到别处。"];
        return NO;
    }
    NSString *python = FindPython();
    AppendLog([NSString stringWithFormat:@"[server] python=%@", python ?: @"没找到"]);
    if (!python) {
        [self showError:@"没有找到 python3"
                 detail:@"先装 Python 3.9 或更新版本：https://www.python.org/downloads/macos/"];
        return NO;
    }
    self.port = FreeLoopbackPort();
    if (self.port <= 0) {
        [self showError:@"找不到可用端口" detail:@"重启一次电脑再试。"];
        return NO;
    }
    NSString *dataDir = [NSHomeDirectory() stringByAppendingPathComponent:@"Library/Application Support/XuhuanSearch"];
    [[NSFileManager defaultManager] createDirectoryAtPath:dataDir withIntermediateDirectories:YES attributes:nil error:nil];
    gLogPath = [NSHomeDirectory() stringByAppendingPathComponent:@"Library/Logs/XuhuanSearch.log"];

    gServer = [[NSTask alloc] init];
    gServer.executableURL = [NSURL fileURLWithPath:python];
    gServer.arguments = @[
        @"-u", serverPath,
        @"--port", [NSString stringWithFormat:@"%d", self.port],
        @"--no-open",
        @"--data-dir", dataDir,
        @"--parent-pid", [NSString stringWithFormat:@"%d", NSProcessInfo.processInfo.processIdentifier],
    ];
    gServer.currentDirectoryURL = [NSURL fileURLWithPath:[serverPath stringByDeletingLastPathComponent]];
    NSFileManager *fm = [NSFileManager defaultManager];
    if (![fm fileExistsAtPath:gLogPath]) [fm createFileAtPath:gLogPath contents:[NSData data] attributes:nil];
    NSFileHandle *logHandle = [NSFileHandle fileHandleForWritingAtPath:gLogPath];
    if (logHandle) {
        [logHandle seekToEndOfFile];
        gServer.standardOutput = logHandle;
        gServer.standardError = logHandle;
    }
    NSError *error = nil;
    if (![gServer launchAndReturnError:&error]) {
        AppendLog([NSString stringWithFormat:@"[server] 启动失败：%@", error.localizedDescription ?: @"未知错误"]);
        [self showError:@"本地服务启动失败" detail:error.localizedDescription ?: @"未知错误"];
        return NO;
    }
    AppendLog([NSString stringWithFormat:@"[server] 服务进程已拉起 pid=%d 端口=%d python=%@",
               gServer.processIdentifier, self.port, python.lastPathComponent]);
    self.pageURL = [NSString stringWithFormat:@"http://127.0.0.1:%d/", self.port];
    return YES;
}

- (void)pollHealth {
    self.tries += 1;
    if (self.tries > 60) {
        [self.pollTimer invalidate]; self.pollTimer = nil;
        [self showError:@"本地服务没有响应"
                 detail:[NSString stringWithFormat:@"看了 24 秒还没起来。日志：%@", gLogPath ?: @"~/Library/Logs/XuhuanSearch.log"]];
        return;
    }
    if (!gServer || !gServer.isRunning) {
        [self.pollTimer invalidate]; self.pollTimer = nil;
        [self showError:@"本地服务已经退出"
                 detail:[NSString stringWithFormat:@"日志：%@", gLogPath ?: @"~/Library/Logs/XuhuanSearch.log"]];
        return;
    }
    NSString *health = [NSString stringWithFormat:@"http://127.0.0.1:%d/api/health", self.port];
    NSURLSessionDataTask *task = [[NSURLSession sharedSession] dataTaskWithURL:[NSURL URLWithString:health]
        completionHandler:^(NSData *data, NSURLResponse *response, NSError *err) {
        if ([(NSHTTPURLResponse *)response statusCode] == 200) {
            dispatch_async(dispatch_get_main_queue(), ^{
                [self.pollTimer invalidate]; self.pollTimer = nil;
                [self loadApp];
            });
        }
    }];
    [task resume];
}

- (void)loadApp {
    NSURLRequest *request = [NSURLRequest requestWithURL:[NSURL URLWithString:self.pageURL]
                                             cachePolicy:NSURLRequestReloadIgnoringLocalCacheData
                                         timeoutInterval:20];
    [self.webView loadRequest:request];
}

#pragma mark - 网页 → 原生
- (void)userContentController:(WKUserContentController *)controller
      didReceiveScriptMessage:(WKScriptMessage *)message {
    if (![message.body isKindOfClass:[NSDictionary class]]) return;
    NSDictionary *body = message.body;
    NSString *cmd = body[@"cmd"];
    if ([cmd isEqualToString:@"drag"]) {
        // 优先交给系统自己拖：跟手、无延迟，鼠标一松就停。
        // 只有拿不到鼠标事件时才退回定时器那套（老办法，会有一点点滞后）。
        NSEvent *event = [NSApp currentEvent];
        if (event && (event.type == NSEventTypeLeftMouseDown || event.type == NSEventTypeLeftMouseDragged)) {
            @try {
                [self.panel performWindowDragWithEvent:event];
                return;
            } @catch (NSException *exception) {
                AppendLog([NSString stringWithFormat:@"[drag] 系统拖动失败：%@", exception.reason]);
            }
        }
        [self.drag beginWithWindow:self.panel];
        return;
    }
    if ([cmd isEqualToString:@"hide"]) {
        [self.panel orderOut:nil];
        return;
    }
    if ([cmd isEqualToString:@"pin"]) {
        self.pinned = [body[@"on"] boolValue];
        self.panel.level = self.pinned ? NSFloatingWindowLevel : NSNormalWindowLevel;
        return;
    }
    if ([cmd isEqualToString:@"open"]) {
        NSString *url = body[@"url"];
        if ([url isKindOfClass:[NSString class]] && url.length) {
            NSURL *target = [NSURL URLWithString:url];
            if (target) [[NSWorkspace sharedWorkspace] openURL:target];
        }
        return;
    }
    if ([cmd isEqualToString:@"dock"]) {
        BOOL on = [body[@"on"] boolValue];
        NSString *corner = [body[@"corner"] isKindOfClass:[NSString class]] ? body[@"corner"] : @"br";
        [self applyDock:on corner:corner];
        return;
    }
    if ([cmd isEqualToString:@"size"]) {
        [self applyContentHeight:[body[@"height"] doubleValue]];
        return;
    }
    if ([cmd isEqualToString:@"focus"]) {
        // 本轮第一次有新结果时，把面板真正顶到别的 app 窗口之上。
        // pin=关 时面板是 NSNormalWindowLevel；orderFrontRegardless 只在本 app
        // 内部排序，跨 app 盖不过去（日志里前台是访达时就是这个情况）。
        // 这里临时抬到浮动层级 3 秒，配合网页的浮起动画让她一眼看到；
        // 不 activate、不 makeKey，不抢她正在别的应用里打字的光标。
        NSInteger restoreLevel = self.pinned ? NSFloatingWindowLevel : NSNormalWindowLevel;
        [self.panel setLevel:NSFloatingWindowLevel + 1];
        [self.panel orderFrontRegardless];
        AppendLog([NSString stringWithFormat:@"[focus] 词=%@ 层级 %ld→%ld 前台=%@",
                   body[@"term"] ?: @"?", (long)restoreLevel, (long)self.panel.level,
                   [NSWorkspace sharedWorkspace].frontmostApplication.localizedName ?: @"?"]);
        __weak SearchApp *weakSelf = self;
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(3 * NSEC_PER_SEC)),
                       dispatch_get_main_queue(), ^{
            [weakSelf.panel setLevel:restoreLevel];   // 3 秒后按 pin 状态恢复，不永久改 pin 语义
        });
        return;
    }
    if ([cmd isEqualToString:@"quit"]) { [NSApp terminate:nil]; return; }
}

#pragma mark - 吸附到命令方块（左/右下角的小窗）

// 网页报多高，窗口就多高：内容少就短，内容多就长，顶边不动只往下伸/往上收
- (void)applyContentHeight:(CGFloat)height {
    if (self.docked) return;                 // 吸附在命令方块里是固定尺寸，不跟着变
    if (height < 100) return;
    NSRect screen = [NSScreen mainScreen].visibleFrame;
    CGFloat maxH = screen.size.height - 40;
    CGFloat h = screen.size.height - 40;
    NSRect f = self.panel.frame;
    if (fabs(h - f.size.height) < 2) return;
    CGFloat y = NSMaxY(f) - h;
    if (y < screen.origin.y + 8) y = screen.origin.y + 8;
    [self.panel setFrame:NSMakeRect(f.origin.x, y, f.size.width, h) display:YES animate:NO];
}

- (NSRect)commandBlockFrame {
    NSRect found = NSZeroRect;
    CFArrayRef list = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID);
    if (!list) return found;
    for (NSDictionary *window in (__bridge NSArray *)list) {
        NSString *owner = window[(id)kCGWindowOwnerName];
        if (![owner isKindOfClass:[NSString class]]) continue;
        if ([owner rangeOfString:@"命令方块"].location == NSNotFound) continue;
        NSDictionary *bounds = window[(id)kCGWindowBounds];
        if (![bounds isKindOfClass:[NSDictionary class]]) continue;
        CGFloat width = [bounds[@"Width"] doubleValue];
        CGFloat height = [bounds[@"Height"] doubleValue];
        if (width < 500 || height < 360) continue;      // 图标、弹层之类的不算
        CGFloat screenHeight = [NSScreen mainScreen].frame.size.height;
        found = NSMakeRect([bounds[@"X"] doubleValue],
                           screenHeight - [bounds[@"Y"] doubleValue] - height,
                           width, height);
        break;
    }
    CFRelease(list);
    return found;
}

- (NSRect)dockFrameForCorner:(NSString *)corner {
    CGFloat width = 340, height = 430;
    NSRect host = [self commandBlockFrame];
    NSRect screen = [NSScreen mainScreen].visibleFrame;
    if (NSEqualRects(host, NSZeroRect)) host = screen;
    BOOL left = [corner isEqualToString:@"bl"];
    CGFloat x = left ? NSMinX(host) + 18 : NSMaxX(host) - width - 18;
    CGFloat y = NSMinY(host) + 18;
    x = MAX(NSMinX(screen) + 8, MIN(x, NSMaxX(screen) - width - 8));
    y = MAX(NSMinY(screen) + 8, MIN(y, NSMaxY(screen) - height - 8));
    return NSMakeRect(x, y, width, height);
}

- (void)applyDock:(BOOL)on corner:(NSString *)corner {
    if (on) {
        if (!self.docked) {
            self.dockSaved = self.panel.frame;
            self.docked = YES;
        }
        [self.panel setFrame:[self dockFrameForCorner:corner] display:YES animate:YES];
    } else if (self.docked) {
        NSRect back = self.dockSaved;
        if (back.size.width < 200 || back.size.height < 200) {
            back = NSMakeRect(NSMaxX([NSScreen mainScreen].visibleFrame) - 344,
                              NSMaxY([NSScreen mainScreen].visibleFrame) - 732, 320, 720);
        }
        [self.panel setFrame:back display:YES animate:YES];
        self.docked = NO;
    }
}

#pragma mark - 菜单栏
- (void)setupStatusItem {
    self.statusItem = [[NSStatusBar systemStatusBar] statusItemWithLength:NSVariableStatusItemLength];
    NSImage *image = nil;
    if (@available(macOS 11.0, *)) {
        image = [NSImage imageWithSystemSymbolName:@"text.magnifyingglass" accessibilityDescription:@"search"];
    }
    if (image) {
        image.template = YES;
        self.statusItem.button.image = image;
    } else {
        self.statusItem.button.title = @"查";
    }
    NSMenu *menu = [[NSMenu alloc] init];
    [menu addItemWithTitle:@"显示 / 收起" action:@selector(togglePanel) keyEquivalent:@""].target = self;
    [menu addItemWithTitle:@"重新载入页面" action:@selector(reloadPage) keyEquivalent:@""].target = self;
    [menu addItem:[NSMenuItem separatorItem]];
    NSMenuItem *dotItem = [[NSMenuItem alloc] initWithTitle:@"划词时出现小圆点"
                                                     action:@selector(toggleSelectionDot:)
                                              keyEquivalent:@""];
    dotItem.target = self;
    dotItem.state = self.selWatcher.on ? NSControlStateValueOn : NSControlStateValueOff;
    [menu addItem:dotItem];
    [menu addItem:[NSMenuItem separatorItem]];
    [menu addItemWithTitle:@"在浏览器里打开" action:@selector(openInBrowser) keyEquivalent:@""].target = self;
    [menu addItemWithTitle:@"打开 命令方块" action:@selector(openCommandBlock) keyEquivalent:@""].target = self;
    [menu addItem:[NSMenuItem separatorItem]];
    [menu addItemWithTitle:@"退出 search" action:@selector(terminate:) keyEquivalent:@"q"];
    self.statusItem.menu = menu;
}

- (void)togglePanel {
    if (self.panel.isVisible && self.panel.isKeyWindow) {
        [self.panel orderOut:nil];
    } else {
        [self.panel orderFrontRegardless];
        [self.panel makeKeyWindow];
    }
}

- (void)reloadPage {
    if (self.pageURL) [self.webView loadRequest:[NSURLRequest requestWithURL:[NSURL URLWithString:self.pageURL]]];
}

- (void)openInBrowser {
    if (self.pageURL) [[NSWorkspace sharedWorkspace] openURL:[NSURL URLWithString:self.pageURL]];
}

- (void)openCommandBlock {
    NSString *path = [NSHomeDirectory() stringByAppendingPathComponent:@"Documents/xuhuan mac/命令方块/命令方块.app"];
    if ([[NSFileManager defaultManager] fileExistsAtPath:path]) {
        [[NSWorkspace sharedWorkspace] openURL:[NSURL fileURLWithPath:path]];
    } else {
        NSAlert *alert = [[NSAlert alloc] init];
        alert.messageText = @"没有找到 命令方块.app";
        alert.informativeText = @"它应该在 Documents/xuhuan mac/命令方块/ 里。找不到就先单独打开它，或者把路径补回来。";
        [alert addButtonWithTitle:@"知道了"];
        [alert runModal];
    }
}

- (void)registerHotKey {
    EventHotKeyRef ref = NULL;
    EventHotKeyID hotKeyId = {kHotKeySignature, kHotKeyId};
    RegisterEventHotKey(kVK_ANSI_S, controlKey | optionKey, hotKeyId, GetApplicationEventTarget(), 0, &ref);
    EventTypeSpec spec = {kEventClassKeyboard, kEventHotKeyPressed};
    InstallApplicationEventHandler(NewEventHandlerUPP((EventHandlerProcPtr)hotKeyHandler), 1, &spec,
                                   (__bridge void *)self, NULL);
}

static OSStatus hotKeyHandler(EventHandlerCallRef nextHandler, EventRef event, void *userData) {
    SearchApp *app = (__bridge SearchApp *)userData;
    dispatch_async(dispatch_get_main_queue(), ^{ [app togglePanel]; });
    return noErr;
}

#pragma mark - 菜单
- (void)setupMenu {
    NSMenu *mainMenu = [[NSMenu alloc] init];
    NSMenuItem *appItem = [[NSMenuItem alloc] init];
    [mainMenu addItem:appItem];
    NSMenu *appMenu = [[NSMenu alloc] init];
    [appMenu addItemWithTitle:@"关于 search" action:@selector(orderFrontStandardAboutPanel:) keyEquivalent:@""];
    [appMenu addItem:[NSMenuItem separatorItem]];
    [appMenu addItemWithTitle:@"隐藏" action:@selector(hide:) keyEquivalent:@"h"];
    [appMenu addItemWithTitle:@"退出 search" action:@selector(terminate:) keyEquivalent:@"q"];
    appItem.submenu = appMenu;

    NSMenuItem *editItem = [[NSMenuItem alloc] init];
    [mainMenu addItem:editItem];
    NSMenu *editMenu = [[NSMenu alloc] initWithTitle:@"编辑"];
    [editMenu addItemWithTitle:@"撤销" action:@selector(undo:) keyEquivalent:@"z"];
    [editMenu addItemWithTitle:@"剪切" action:@selector(cut:) keyEquivalent:@"x"];
    [editMenu addItemWithTitle:@"拷贝" action:@selector(copy:) keyEquivalent:@"c"];
    [editMenu addItemWithTitle:@"粘贴" action:@selector(paste:) keyEquivalent:@"v"];
    [editMenu addItemWithTitle:@"全选" action:@selector(selectAll:) keyEquivalent:@"a"];
    editItem.submenu = editMenu;
    NSApp.mainMenu = mainMenu;
}

#pragma mark - 兜底页面
- (void)showLoading:(NSString *)text {
    NSString *html = [NSString stringWithFormat:
        @"<!doctype html><meta charset='utf-8'>"
         "<style>body{margin:0;height:100vh;display:grid;place-items:center;background:#f4f1e9;"
         "font-family:-apple-system,'PingFang SC',sans-serif;color:#8a8e93}"
         ".ring{width:22px;height:22px;margin:0 auto 12px;border-radius:50%%;"
         "border:2px solid #e6dfd0;border-top-color:#c0472a;animation:s .9s linear infinite}"
         "@keyframes s{to{transform:rotate(360deg)}}p{font-size:11.5px;letter-spacing:.02em;margin:0}</style>"
         "<div><div class='ring'></div><p>%@</p></div>", text];
    [self.webView loadHTMLString:html baseURL:nil];
}

- (void)showError:(NSString *)title detail:(NSString *)detail {
    NSString *html = [NSString stringWithFormat:
        @"<!doctype html><meta charset='utf-8'>"
         "<style>body{margin:0;height:100vh;display:grid;place-items:center;background:#f4f1e9;"
         "font-family:-apple-system,'PingFang SC',sans-serif;color:#1c2025;padding:18px}"
         ".card{max-width:340px;padding:20px;background:#fffdf7;border:1px solid #e6dfd0;border-radius:12px}"
         ".tag{display:inline-block;margin-bottom:9px;padding:2px 7px;border-radius:4px;"
         "background:#fbece6;color:#c0472a;font-size:9.5px;letter-spacing:.06em}"
         "h1{margin:0 0 6px;font-size:14px}p{margin:0;font-size:11.5px;line-height:1.7;color:#565c64}</style>"
         "<div class='card'><span class='tag'>启动失败</span><h1>%@</h1><p>%@</p></div>", title, detail];
    [self.webView loadHTMLString:html baseURL:nil];
}

#pragma mark - 生命周期
- (void)applicationWillTerminate:(NSNotification *)notification {
    if (self.pollTimer) [self.pollTimer invalidate];
    if (gServer && gServer.isRunning) {
        AppendLog(@"[launcher] terminating server");
        [gServer terminate];
        [gServer waitUntilExit];
    }
}

- (BOOL)applicationShouldTerminateAfterLastWindowClosed:(NSApplication *)sender { return NO; }

@end

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        NSApplication *app = [NSApplication sharedApplication];
        [app setActivationPolicy:NSApplicationActivationPolicyAccessory];
        SearchApp *delegate = [[SearchApp alloc] init];
        delegate.drag = [[DragBridge alloc] init];
        app.delegate = delegate;
        [app run];
    }
    return 0;
}

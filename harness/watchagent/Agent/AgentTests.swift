import WatchKit
import XCTest

/// One endless UI test that serves the harness (harness/watch.py) through files in $TH_WATCH_AGENT_DIR:
/// the harness writes cmd-<id>.json ({"method", "params"}), the agent answers res-<id>.json ({"ok", "result"|"error"}).
/// Files rather than a socket: a simulator process can always reach the Mac's file system.
final class AgentTests: XCTestCase {
    fileprivate var issues: [String] = []

    override func setUp() {
        continueAfterFailure = true
    }

    // XCUITest reports a failed interaction as an issue rather than an error; the agent returns it to the harness.
    override func record(_ issue: XCTIssue) {
        issues.append(issue.compactDescription)
        super.record(issue)
    }

    func testServe() throws {
        guard let dir = ProcessInfo.processInfo.environment["TH_WATCH_AGENT_DIR"] else {
            XCTFail("TH_WATCH_AGENT_DIR is not set (xcodebuild passes TEST_RUNNER_TH_WATCH_AGENT_DIR)")
            return
        }
        try Agent(dir: URL(fileURLWithPath: dir), test: self).serve()
    }
}

private enum AgentError: Error, CustomStringConvertible {
    case badRequest(String)
    case failed(String)

    var description: String {
        switch self {
        case let .badRequest(message), let .failed(message): return message
        }
    }
}

private final class Agent {
    static let carousel = "com.apple.Carousel"

    let dir: URL
    unowned let test: AgentTests
    var bundleId = Agent.carousel
    let fm = FileManager.default

    init(dir: URL, test: AgentTests) {
        self.dir = dir
        self.test = test
    }

    func serve() throws {
        try fm.createDirectory(at: dir, withIntermediateDirectories: true)
        try write("ready.json", ["pid": Int(getpid()), "screen": screen()])
        // from its own thread: an XCUITest action can hold the main thread for 20 s and more (Home, a launch)
        let beat = Thread { [dir] in
            while true {
                let tmp = dir.appendingPathComponent(".alive.json.tmp")
                if let data = try? JSONSerialization.data(withJSONObject: ["at": Date().timeIntervalSince1970]),
                   (try? data.write(to: tmp)) != nil {
                    rename(tmp.path, dir.appendingPathComponent("alive.json").path)
                }
                Thread.sleep(forTimeInterval: 1)
            }
        }
        beat.start()
        while true {
            let names = ((try? fm.contentsOfDirectory(atPath: dir.path)) ?? [])
                .filter { $0.hasPrefix("cmd-") && $0.hasSuffix(".json") }
                .sorted()
            if names.isEmpty {
                Thread.sleep(forTimeInterval: 0.05)
                continue
            }
            for name in names {
                let url = dir.appendingPathComponent(name)
                guard let data = try? Data(contentsOf: url) else { continue }
                try? fm.removeItem(at: url)
                let id = String(name.dropFirst(4).dropLast(5))
                let request = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
                let method = request["method"] as? String ?? ""
                let params = request["params"] as? [String: Any] ?? [:]
                test.issues.removeAll()
                var response: [String: Any]
                do {
                    let result = try handle(method, params)
                    if test.issues.isEmpty {
                        response = ["ok": true, "result": result]
                    } else {
                        response = ["ok": false, "error": test.issues.joined(separator: "; ")]
                    }
                } catch {
                    response = ["ok": false, "error": "\(error)"]
                }
                try write("res-\(id).json", response)
                if method == "quit" { return }
            }
        }
    }

    // MARK: - Commands (coordinates are points)

    func handle(_ method: String, _ p: [String: Any]) throws -> Any {
        switch method {
        case "ping":
            return ["bundleId": bundleId]
        case "screen":
            return screen()
        case "launch":
            // Fails the test, and so ends the agent, when the launch cannot complete; the harness launches with simctl
            // and then calls activate.
            let id = try string(p, "bundleId")
            let app = XCUIApplication(bundleIdentifier: id)
            app.launch()
            bundleId = id
            return state(app)
        case "activate":
            let id = try string(p, "bundleId")
            let app = XCUIApplication(bundleIdentifier: id)
            app.activate()
            bundleId = id
            return state(app)
        case "terminate":
            let id = try string(p, "bundleId")
            XCUIApplication(bundleIdentifier: id).terminate()
            if id == bundleId { bundleId = Agent.carousel }
            return true
        case "focus":
            // After an agent restart: the app the harness last launched, without relaunching it.
            bundleId = try string(p, "bundleId")
            return state(XCUIApplication(bundleIdentifier: bundleId))
        case "device":
            // What an app reads from WatchKit: Trio picks its layout by screenBounds and its crown icon by wristLocation.
            let d = WKInterfaceDevice.current()
            let b = d.screenBounds
            return [
                "wrist": d.wristLocation == .left ? "left" : "right",
                "crown": d.crownOrientation == .left ? "left" : "right",
                "width": Double(b.width), "height": Double(b.height), "scale": Double(d.screenScale),
                "model": d.model, "systemVersion": d.systemVersion,
                "contentSize": d.preferredContentSizeCategory
            ]
        case "state":
            return state(XCUIApplication(bundleIdentifier: (p["bundleId"] as? String) ?? bundleId))
        case "tap":
            coordinate(try number(p, "x"), try number(p, "y")).tap()
            return true
        case "tapElement":
            let text = try string(p, "text")
            let format = (p["exact"] as? Bool) == false
                ? "label CONTAINS[c] %@ OR identifier CONTAINS[c] %@"
                : "label == %@ OR identifier == %@"
            let matches = foreground().descendants(matching: (p["buttonsOnly"] as? Bool) == true ? .button : .any)
                .matching(NSPredicate(format: format, text, text))
            let element = matches.element(boundBy: (p["index"] as? Int) ?? 0)
            guard element.waitForExistence(timeout: (p["timeout"] as? Double) ?? 5) else {
                throw AgentError.failed("no element matching \(text)")
            }
            element.tap()
            return true
        case "press":
            coordinate(try number(p, "x"), try number(p, "y")).press(forDuration: (p["duration"] as? Double) ?? 1.0)
            return true
        case "drag":
            let from = coordinate(try number(p, "x1"), try number(p, "y1"))
            let to = coordinate(try number(p, "x2"), try number(p, "y2"))
            from.press(
                forDuration: (p["hold"] as? Double) ?? 0.05,
                thenDragTo: to,
                withVelocity: XCUIGestureVelocity(rawValue: CGFloat((p["velocity"] as? Double) ?? 1000)),
                thenHoldForDuration: (p["settle"] as? Double) ?? 0.0
            )
            return true
        case "crown":
            // 1.0 is one full turn of the crown; the sign is the direction.
            let delta = CGFloat(try number(p, "delta"))
            if let velocity = p["velocity"] as? Double {
                XCUIDevice.shared.rotateDigitalCrown(delta: delta, velocity: XCUIGestureVelocity(rawValue: CGFloat(velocity)))
            } else {
                XCUIDevice.shared.rotateDigitalCrown(delta: delta)
            }
            return true
        case "home":
            XCUIDevice.shared.press(.home)
            bundleId = Agent.carousel
            return true
        case "type":
            foreground().typeText(try string(p, "text"))
            return true
        case "tree":
            let snapshot = try foreground().snapshot()
            var nodes: [[String: Any]] = []
            flatten(snapshot, into: &nodes)
            return nodes
        case "quit":
            return true
        default:
            throw AgentError.badRequest("unknown method \(method)")
        }
    }

    // MARK: - Helpers

    /// The app the harness last launched or activated if it is in front, else the watch face / app grid.
    func foreground() -> XCUIApplication {
        let app = XCUIApplication(bundleIdentifier: bundleId)
        if bundleId != Agent.carousel, app.state == .runningForeground { return app }
        return XCUIApplication(bundleIdentifier: Agent.carousel)
    }

    func coordinate(_ x: Double, _ y: Double) -> XCUICoordinate {
        foreground().coordinate(withNormalizedOffset: CGVector(dx: 0, dy: 0)).withOffset(CGVector(dx: x, dy: y))
    }

    func screen() -> [String: Any] {
        let shot = XCUIScreen.main.screenshot().image
        return ["width": Double(shot.size.width), "height": Double(shot.size.height), "scale": Double(shot.scale)]
    }

    func state(_ app: XCUIApplication) -> [String: Any] {
        let names: [XCUIApplication.State: String] = [
            .unknown: "unknown", .notRunning: "notRunning", .runningBackgroundSuspended: "suspended",
            .runningBackground: "background", .runningForeground: "foreground"
        ]
        return ["bundleId": bundleId, "state": names[app.state] ?? "unknown"]
    }

    func flatten(_ s: XCUIElementSnapshot, into nodes: inout [[String: Any]]) {
        let f = s.frame
        if f.width > 0, f.height > 0 {
            nodes.append([
                "label": s.label,
                "value": s.value.map { "\($0)" } ?? "",
                "identifier": s.identifier,
                "role": "XCUIElementType" + typeName(s.elementType),
                "enabled": s.isEnabled,
                "focused": s.hasFocus,
                "x": Double(f.minX), "y": Double(f.minY), "w": Double(f.width), "h": Double(f.height)
            ])
        }
        for child in s.children { flatten(child, into: &nodes) }
    }

    func string(_ p: [String: Any], _ key: String) throws -> String {
        guard let v = p[key] as? String else { throw AgentError.badRequest("missing \(key)") }
        return v
    }

    func number(_ p: [String: Any], _ key: String) throws -> Double {
        guard let v = p[key] as? NSNumber else { throw AgentError.badRequest("missing \(key)") }
        return v.doubleValue
    }

    func write(_ name: String, _ object: Any) throws {
        let data = try JSONSerialization.data(withJSONObject: object)
        let tmp = dir.appendingPathComponent(".\(name).tmp")
        try data.write(to: tmp)
        // rename(2) replaces the file atomically: a remove and a move left a moment with no alive.json, which the
        // harness read as a stopped agent
        let dest = dir.appendingPathComponent(name)
        guard rename(tmp.path, dest.path) == 0 else {
            throw AgentError.failed("rename \(name): \(String(cString: strerror(errno)))")
        }
    }

    func typeName(_ t: XCUIElement.ElementType) -> String {
        switch t {
        case .application: return "Application"
        case .window: return "Window"
        case .alert: return "Alert"
        case .button: return "Button"
        case .navigationBar: return "NavigationBar"
        case .table: return "Table"
        case .cell: return "Cell"
        case .collectionView: return "CollectionView"
        case .scrollView: return "ScrollView"
        case .staticText: return "StaticText"
        case .image: return "Image"
        case .icon: return "Icon"
        case .slider: return "Slider"
        case .picker: return "Picker"
        case .pickerWheel: return "PickerWheel"
        case .switch: return "Switch"
        case .toggle: return "Toggle"
        case .progressIndicator: return "ProgressIndicator"
        case .activityIndicator: return "ActivityIndicator"
        case .pageIndicator: return "PageIndicator"
        case .textField: return "TextField"
        case .secureTextField: return "SecureTextField"
        case .link: return "Link"
        case .toolbar: return "Toolbar"
        case .menu: return "Menu"
        case .menuItem: return "MenuItem"
        case .sheet: return "Sheet"
        case .group: return "Group"
        case .map: return "Map"
        case .other: return "Other"
        default: return "Other\(t.rawValue)"
        }
    }
}

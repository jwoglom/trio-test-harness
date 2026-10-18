import SwiftUI

// XCUITest needs a target app to host the UI test runner; the agent drives other apps by bundle id.
@main
struct HostApp: App {
    var body: some Scene {
        WindowGroup { Text("th watch agent") }
    }
}

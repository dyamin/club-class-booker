import AppKit
import ApplicationServices

// Reads and drives the club app through macOS accessibility. Positions are in window points
// (the window's top-left corner is 0,0), the same space as the taps of the screen tool.
//   ax dump <process>                               JSON: the window and its elements
//   ax press <process> <role> <description> <y> <tolerance>   press the nearest element of that role and exact description
//   ax press-in-card <process> <card description> <button description>   press the button of that text that lies
//                                                   inside the ONE card of that exact text (refuses 0 or 2 cards)
//   ax scroll <process> down|up                     scroll the biggest scroll area by one page

func attr(_ el: AXUIElement, _ name: String) -> CFTypeRef? {
    var value: CFTypeRef?
    return AXUIElementCopyAttributeValue(el, name as CFString, &value) == .success ? value : nil
}
func text(_ el: AXUIElement, _ name: String) -> String { (attr(el, name) as? String) ?? "" }
func point(_ el: AXUIElement) -> CGPoint {
    var p = CGPoint.zero
    if let v = attr(el, kAXPositionAttribute as String) { AXValueGetValue(v as! AXValue, .cgPoint, &p) }
    return p
}
func size(_ el: AXUIElement) -> CGSize {
    var s = CGSize.zero
    if let v = attr(el, kAXSizeAttribute as String) { AXValueGetValue(v as! AXValue, .cgSize, &s) }
    return s
}
func fail(_ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(1)
}

struct Item: Codable { let role: String; let desc: String; let value: String; let x: Double; let y: Double; let w: Double; let h: Double }
struct Dump: Codable { let window: Item; let elements: [Item] }

func windowOf(_ name: String) -> AXUIElement {
    guard let app = NSWorkspace.shared.runningApplications.first(where: {
        $0.executableURL?.lastPathComponent == name || $0.localizedName == name }) else { fail("the app \(name) is not running") }
    let root = AXUIElementCreateApplication(app.processIdentifier)
    if let w = attr(root, kAXFocusedWindowAttribute as String) { return (w as! AXUIElement) }
    if let list = attr(root, kAXWindowsAttribute as String) as? [AXUIElement], let first = list.first { return first }
    fail("the app \(name) has no window (is accessibility allowed for this program?)")
}

var found: [(AXUIElement, Item)] = []
func walk(_ el: AXUIElement, _ origin: CGPoint, _ depth: Int) {
    if depth > 40 || found.count > 3000 { return }
    let role = text(el, kAXRoleAttribute as String)
    let desc = text(el, kAXDescriptionAttribute as String)
    let value = text(el, kAXValueAttribute as String)
    let keep = !desc.isEmpty || !value.isEmpty || role == "AXButton" || role == "AXScrollArea" || role == "AXTextField"
    if keep {
        let p = point(el), s = size(el)
        found.append((el, Item(role: role, desc: desc, value: value, x: Double(p.x - origin.x), y: Double(p.y - origin.y),
                               w: Double(s.width), h: Double(s.height))))
    }
    if let kids = attr(el, kAXChildrenAttribute as String) as? [AXUIElement] {
        for k in kids { walk(k, origin, depth + 1) }
    }
}
func collect(_ name: String) -> Item {
    let win = windowOf(name)
    let p = point(win), s = size(win)
    found = []
    walk(win, p, 0)
    return Item(role: "AXWindow", desc: text(win, kAXTitleAttribute as String), value: "", x: Double(p.x), y: Double(p.y),
                w: Double(s.width), h: Double(s.height))
}

let args = CommandLine.arguments
guard args.count >= 3 else { fail("usage: ax dump|press|scroll <process> ...") }
let command = args[1], name = args[2]
switch command {
case "dump":
    let window = collect(name)
    let data = try JSONEncoder().encode(Dump(window: window, elements: found.map { $0.1 }))
    print(String(data: data, encoding: .utf8)!)
case "press":
    guard args.count >= 7, let wantY = Double(args[5]), let tol = Double(args[6]) else { fail("usage: ax press <process> <role> <description> <y> <tolerance>") }
    _ = collect(name)
    let candidates = found.filter { $0.1.role == args[3] && $0.1.desc == args[4] && abs($0.1.y + $0.1.h / 2 - wantY) <= tol }
    guard let best = candidates.min(by: { abs($0.1.y + $0.1.h / 2 - wantY) < abs($1.1.y + $1.1.h / 2 - wantY) }) else { fail("no such element") }
    if AXUIElementPerformAction(best.0, kAXPressAction as CFString) != .success { fail("the press failed") }
    print("pressed")
case "press-in-card":
    guard args.count >= 5 else { fail("usage: ax press-in-card <process> <card description> <button description>") }
    _ = collect(name)
    let cards = found.filter { $0.1.role == "AXButton" && $0.1.desc == args[3] }
    guard cards.count == 1 else { fail("the card was found \(cards.count) times, not once") }
    let card = cards[0].1
    let inside = found.filter { $0.1.role == "AXButton" && $0.1.desc == args[4] && $0.1.h > 0
        && $0.1.y >= card.y - 1 && $0.1.y + $0.1.h <= card.y + card.h + 1 }
    guard let button = inside.first else { fail("no button \(args[4]) inside that card") }
    if AXUIElementPerformAction(button.0, kAXPressAction as CFString) != .success { fail("the press failed") }
    print("pressed")
case "scroll":
    guard args.count >= 4, ["down", "up"].contains(args[3]) else { fail("usage: ax scroll <process> down|up") }
    _ = collect(name)
    let areas = found.filter { $0.1.role == "AXScrollArea" }
    guard let area = areas.max(by: { $0.1.w * $0.1.h < $1.1.w * $1.1.h }) else { fail("no scroll area") }
    let action = args[3] == "down" ? "AXScrollDownByPage" : "AXScrollUpByPage"
    if AXUIElementPerformAction(area.0, action as CFString) != .success { fail("the scroll failed") }
    print("scrolled")
default:
    fail("unknown command \(command)")
}

// 字幕の窓: 字幕のファイルの内容を、画面に浮かぶ小さな窓へ表示する。
//
// 使い方: caption <字幕のフォルダ>
// フォルダには日付ごとのファイル(YYYY-MM-DD.txt)が入り、いちばん新しい日のものを表示する。
// 字幕のファイルは 1 行 1 発言で、「時刻<TAB>セッション名<TAB>話し手<TAB>本文」の形(listen.py が書く)。
//
// - どのアプリの上にも浮かび、デスクトップを切り替えてもついてくる
// - 上の帯に、セッションごとのタブが並ぶ。押すと、そのセッションの会話に切り替わる。
//   新しい発言が来ると、そのセッションのタブに自動で切り替わる
// - Claude の発言は青、利用者の発言は黄で色分けする。古い発言は薄く表示する
// - 帯の空いているところをつかむと動かせる。右下のつまみを引くと大きさを変えられる
// - 右上の × で隠せる。隠しても、次の読み上げや発言でまた出る
// - 表示するだけで、どこにも送信しない

import AppKit

guard CommandLine.arguments.count > 1 else {
    FileHandle.standardError.write("使い方: caption <字幕のフォルダ>\n".data(using: .utf8)!)
    exit(2)
}
let captionDir = CommandLine.arguments[1]
let maxShownEntries = 300  // 表示するのは直近の分だけ。ファイルには全部残る
let allTab = "すべて"
let userName = "あなた"
let pollSeconds: TimeInterval = 0.3
let barHeight: CGFloat = 26

struct Entry {
    let time: String
    let session: String
    let who: String
    let text: String
}

/// つかんで窓を動かすための帯
final class DragBar: NSView {
    override func mouseDown(with event: NSEvent) { window?.performDrag(with: event) }
    override func draw(_ dirtyRect: NSRect) {
        NSColor.white.withAlphaComponent(0.10).setFill()
        bounds.fill()
    }
}

/// 右下のつまみ。引くと窓の大きさが変わる(左上の位置は動かさない)
final class ResizeGrip: NSView {
    private var startFrame = NSRect.zero
    private var startMouse = NSPoint.zero
    override func mouseDown(with event: NSEvent) {
        guard let w = window else { return }
        startFrame = w.frame
        startMouse = NSEvent.mouseLocation
    }
    override func mouseDragged(with event: NSEvent) {
        guard let w = window else { return }
        let now = NSEvent.mouseLocation
        let width = max(w.minSize.width, startFrame.width + (now.x - startMouse.x))
        let height = max(w.minSize.height, startFrame.height - (now.y - startMouse.y))
        w.setFrame(NSRect(x: startFrame.minX, y: startFrame.maxY - height, width: width, height: height), display: true)
    }
    override func mouseUp(with event: NSEvent) { window?.saveFrame(usingName: "10-voice-caption") }
    override func resetCursorRects() { addCursorRect(bounds, cursor: .crosshair) }
    override func draw(_ dirtyRect: NSRect) {
        NSColor.white.withAlphaComponent(0.45).setStroke()
        for offset in [4.0, 8.0, 12.0] {  // 斜めの 3 本線
            let path = NSBezierPath()
            path.move(to: NSPoint(x: bounds.maxX - 2, y: offset))
            path.line(to: NSPoint(x: bounds.maxX - offset, y: 2))
            path.lineWidth = 1.2
            path.stroke()
        }
    }
}

@MainActor
final class CaptionController: NSObject {
    let panel: NSPanel
    let bar: DragBar
    let textView: NSTextView
    var tabButtons: [NSButton] = []
    var tabNames: [String] = []
    var selected = allTab
    var lastRaw: String? = nil
    var lastStamp = ""
    var entries: [Entry] = []

    let claudeColor = NSColor(calibratedRed: 0.55, green: 0.80, blue: 1.00, alpha: 1)
    let userColor = NSColor(calibratedRed: 1.00, green: 0.84, blue: 0.45, alpha: 1)

    override init() {
        let screen = (NSScreen.main ?? NSScreen.screens[0]).visibleFrame
        let width = min(900, screen.width * 0.6)
        let height: CGFloat = 200
        let frame = NSRect(x: screen.midX - width / 2, y: screen.minY + 40, width: width, height: height)
        panel = NSPanel(contentRect: frame, styleMask: [.nonactivatingPanel, .borderless, .resizable],
                        backing: .buffered, defer: false)
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.isOpaque = false
        panel.backgroundColor = NSColor(calibratedWhite: 0.07, alpha: 0.88)
        panel.hasShadow = true
        panel.hidesOnDeactivate = false
        panel.minSize = NSSize(width: 320, height: 100)
        panel.setFrameAutosaveName("10-voice-caption")  // 動かした位置と大きさを覚える

        let content = panel.contentView!
        content.wantsLayer = true
        content.layer?.cornerRadius = 12
        content.layer?.masksToBounds = true

        bar = DragBar(frame: NSRect(x: 0, y: content.bounds.height - barHeight,
                                    width: content.bounds.width, height: barHeight))
        bar.autoresizingMask = [.width, .minYMargin]
        content.addSubview(bar)

        let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: content.bounds.width,
                                                height: content.bounds.height - barHeight))
        scroll.autoresizingMask = [.width, .height]
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = false
        textView = NSTextView(frame: scroll.bounds)
        textView.isEditable = false
        textView.isSelectable = true
        textView.drawsBackground = false
        textView.textContainerInset = NSSize(width: 12, height: 8)
        textView.autoresizingMask = [.width]
        textView.isVerticallyResizable = true
        textView.textContainer?.widthTracksTextView = true
        scroll.documentView = textView
        content.addSubview(scroll)

        let grip = ResizeGrip(frame: NSRect(x: content.bounds.width - 18, y: 0, width: 18, height: 18))
        grip.autoresizingMask = [.minXMargin, .maxYMargin]
        grip.toolTip = "引くと大きさを変えられる"
        content.addSubview(grip)

        super.init()

        let close = NSButton(frame: NSRect(x: bar.bounds.width - 28, y: 3, width: 20, height: 20))
        close.title = "×"
        close.isBordered = false
        close.font = NSFont.systemFont(ofSize: 15, weight: .medium)
        close.contentTintColor = NSColor.white.withAlphaComponent(0.8)
        close.target = self
        close.action = #selector(hide(_:))
        close.autoresizingMask = [.minXMargin]
        close.toolTip = "字幕を隠す(次の読み上げや発言でまた出る)"
        bar.addSubview(close)
    }

    @objc func hide(_ sender: Any?) { panel.orderOut(nil) }

    @objc func tabPressed(_ sender: NSButton) {
        selected = sender.identifier?.rawValue ?? allTab
        styleTabs()
        render()
    }

    func parse(_ raw: String) -> [Entry] {
        raw.split(separator: "\n", omittingEmptySubsequences: true).compactMap { line in
            let f = line.split(separator: "\t", maxSplits: 3, omittingEmptySubsequences: false).map(String.init)
            return f.count == 4 ? Entry(time: f[0], session: f[1], who: f[2], text: f[3]) : nil
        }
    }

    func rebuildTabs() {
        var names: [String] = []
        for e in entries where !names.contains(e.session) { names.append(e.session) }
        let wanted = names.count > 1 ? [allTab] + names : names  // セッションが 1 つなら「すべて」は出さない
        if wanted == tabNames { return }
        tabNames = wanted
        tabButtons.forEach { $0.removeFromSuperview() }
        tabButtons = []
        var x: CGFloat = 8
        for name in wanted {
            let b = NSButton(frame: .zero)
            b.title = name
            b.identifier = NSUserInterfaceItemIdentifier(name)
            b.isBordered = false
            b.wantsLayer = true
            b.layer?.cornerRadius = 6
            b.font = NSFont.systemFont(ofSize: 12, weight: .medium)
            b.target = self
            b.action = #selector(tabPressed(_:))
            b.sizeToFit()
            b.frame = NSRect(x: x, y: 3, width: b.frame.width + 14, height: 20)
            x += b.frame.width + 4
            bar.addSubview(b)
            tabButtons.append(b)
        }
        if !wanted.contains(selected) { selected = wanted.first ?? allTab }
        styleTabs()
    }

    func styleTabs() {
        for b in tabButtons {
            let on = b.identifier?.rawValue == selected
            b.layer?.backgroundColor = (on ? NSColor.white.withAlphaComponent(0.22) : NSColor.clear).cgColor
            b.contentTintColor = on ? .white : NSColor.white.withAlphaComponent(0.55)
        }
    }

    func render() {
        let shown = selected == allTab ? entries : entries.filter { $0.session == selected }
        let out = NSMutableAttributedString()
        for (i, e) in shown.enumerated() {
            let isUser = e.who == userName
            let isLatest = i == shown.count - 1
            let dim: CGFloat = isLatest ? 1.0 : 0.62          // 古い発言は薄くする
            let accent = (isUser ? userColor : claudeColor).withAlphaComponent(dim)
            let body = (isUser ? userColor.blended(withFraction: 0.55, of: .white)! : NSColor.white)
                .withAlphaComponent(dim)
            var head = "▍" + e.who
            if selected == allTab && tabNames.count > 1 { head += "  [\(e.session)]" }
            head += "  " + e.time + "\n"
            let headStyle = NSMutableParagraphStyle()
            headStyle.paragraphSpacing = 1
            out.append(NSAttributedString(string: head, attributes: [
                .font: NSFont.systemFont(ofSize: 11, weight: .semibold),
                .foregroundColor: accent, .paragraphStyle: headStyle,
            ]))
            let style = NSMutableParagraphStyle()
            style.paragraphSpacing = 8
            style.lineSpacing = 2
            style.firstLineHeadIndent = 14                     // 本文を、印のぶん下げる
            style.headIndent = 14
            out.append(NSAttributedString(string: e.text + "\n", attributes: [
                .font: NSFont.systemFont(ofSize: 16, weight: isLatest ? .medium : .regular),
                .foregroundColor: body, .paragraphStyle: style,
            ]))
        }
        textView.textStorage?.setAttributedString(out)
        textView.scrollToEndOfDocument(nil)
    }

    func refresh() {
        // いちばん新しい日のファイルを探し、更新されたときだけ読み直す
        let fm = FileManager.default
        let names = ((try? fm.contentsOfDirectory(atPath: captionDir)) ?? []).filter { $0.hasSuffix(".txt") }.sorted()
        guard let name = names.last else {
            if lastRaw == nil { lastRaw = "" }
            return
        }
        let path = captionDir + "/" + name
        let attrs = try? fm.attributesOfItem(atPath: path)
        let stamp = "\(name) \(attrs?[.modificationDate] ?? "") \(attrs?[.size] ?? "")"
        if stamp == lastStamp { return }
        lastStamp = stamp
        let raw = (try? String(contentsOfFile: path, encoding: .utf8)) ?? ""
        if raw == lastRaw { return }
        let first = lastRaw == nil
        lastRaw = raw
        entries = Array(parse(raw).suffix(maxShownEntries))
        rebuildTabs()
        // 新しい発言が来たら、そのセッションのタブへ切り替える(「すべて」を見ているときは動かさない)
        if !first, let last = entries.last, selected != allTab, tabNames.contains(last.session) {
            selected = last.session
            styleTabs()
        }
        render()
        if !entries.isEmpty && (!panel.isVisible || first) { panel.orderFrontRegardless() }
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)  // Dock に出さず、ほかのアプリの操作を奪わない
// 最上位のコードはメインスレッドで動く。メインアクターの中として扱う
let controller = MainActor.assumeIsolated { () -> CaptionController in
    let c = CaptionController()
    c.refresh()
    return c
}
Timer.scheduledTimer(withTimeInterval: pollSeconds, repeats: true) { _ in
    MainActor.assumeIsolated { controller.refresh() }
}
signal(SIGTERM) { _ in exit(0) }
app.run()

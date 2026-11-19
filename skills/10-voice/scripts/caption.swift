// 字幕の窓: 字幕と、セッションの置き場所を、画面に浮かぶ小さな窓へ表示する。
//
// 使い方: caption <字幕のフォルダ> <セッションの記録>
// - 字幕のフォルダには日付ごとのファイル(YYYY-MM-DD.txt)が入り、いちばん新しい日のものを表示する。
//   1 行 1 発言で、「時刻<TAB>セッション名<TAB>話し手<TAB>本文」の形(listen.py が書く)
// - セッションの記録は JSON で、セッションごとの置き場所(いま話す / あとで)と状態、話す相手が入る
//
// 見え方と操作:
// - どのアプリの上にも浮かび、デスクトップを切り替えてもついてくる
// - 上の帯に「いま話す」「あとで」の区画があり、セッションのタブが入る。
//   タブを押すと、そのセッションが話す相手になる。字幕もそのセッションのものに切り替わり、
//   ほかのセッションは、話すのも聞くのもやめて待つ。「すべて」を押すと、相手を選ばない状態に戻り、
//   「いま話す」のセッションが順番に話す。つまんで別の区画へ落とすと、置き場所が変わる
// - タブの丸印は状態を表す。緑: 話している、聞いている。橙: 話したくて待っている
// - Claude の発言は青、利用者の発言は黄で色分けする。古い発言は薄く表示する
// - 帯の空いているところをつかむと動かせる。右下のつまみを引くと大きさを変えられる
// - 右上の停止ボタンを押すと、すべてのセッションが話すのも聞くのもやめる(会議のときなど)。
//   もう一度押すと再開する
// - Claude Code を終了したセッションは、タブから消える
// - 右上の「?」を押すと、使い方の一覧を表示する。もう一度押すと、字幕に戻る
// - 右上の × で隠せる。隠しても、次の読み上げや発言でまた出る
// - 表示と、置き場所の書き換えだけを行う。どこにも送信しない

import AppKit

guard CommandLine.arguments.count > 2 else {
    FileHandle.standardError.write("使い方: caption <字幕のフォルダ> <セッションの記録>\n".data(using: .utf8)!)
    exit(2)
}
let captionDir = CommandLine.arguments[1]
let sessionsPath = CommandLine.arguments[2]
let maxShownEntries = 300  // 表示するのは直近の分だけ。ファイルには全部残る
let allTab = "すべて"
let userName = "あなた"
let pollSeconds: TimeInterval = 0.3
let barHeight: CGFloat = 30
let lanes: [(key: String, title: String)] = [("normal", "いま話す"), ("later", "あとで")]

/// 「?」で表示する使い方。見出しと、操作・説明の組
let helpSections: [(title: String, rows: [(String, String)])] = [
    ("話す", [
        ("右 ⌘ を押し、合図のあとに話す", "離すと、発言が Claude に届く。考えて黙るときは、押したままにする。押していない間、マイクは閉じている"),
        ("読み上げ中に右 ⌘ を押す", "読み上げが止まり、そのまま話せる"),
        ("右 ⌘ +「ミュート」", "マイクを閉じる。再開は /10-voice"),
    ]),
    ("メモ(あとでやること)", [
        ("右 ⌘ +「メモ、(内容)」", "預かって、会話は続く。返事は、キーを離してから、もう一度押して話す"),
        ("Claude の作業中に右 ⌘", "合図が鳴ってから内容を話す。「メモ」と言わなくてよい"),
        ("預かったメモ", "Claude が、そのセッションの TODO.md に書き足す"),
    ]),
    ("タブ", [
        ("タブを押す", "そのセッションが話す相手になる。ほかのセッションは止まって待つ"),
        ("「すべて」を押す", "相手を選ばない。「いま話す」のセッションが順番に話す"),
        ("タブをつまんで移す", "「あとで」に置くと、戻すまで話しかけてこない"),
        ("丸の色", "緑: 会話中　橙: 話したくて待っている　灰: 作業中など"),
    ]),
    ("窓", [
        ("⏸ 停止 / ▶ 再開", "すべてのセッションが、話すのも聞くのもやめる(会議のとき)"),
        ("×", "隠す。次の読み上げや発言でまた出る"),
        ("帯をつかむ / 右下のつまみ", "動かす / 大きさを変える"),
    ]),
    ("合図の音", [
        ("ピン", "マイクが開いた。話せる"),
        ("グラス", "発言を受け付けた"),
        ("ポン", "メモを預かった"),
        ("低い音", "聞き取れなかった。もう一度、押して話す"),
    ]),
    ("始め方と終わり方", [
        ("/10-voice", "音声で会話する(キーを押して話す)"),
        ("/10-voice hands", "キーを使わない。「以上」と言って話し終える"),
        ("/10-voice off", "サービスを止める(使わない日)"),
        ("「終わり」と言う", "音声の会話を終える"),
    ]),
]

struct Entry {
    let time: String
    let session: String
    let who: String
    let text: String
}

struct SessionInfo: Equatable {
    var lane: String
    var state: String
}

/// 停止ボタンが押されているかを読む
func loadPaused() -> Bool {
    guard let data = FileManager.default.contents(atPath: sessionsPath),
          let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return false }
    return root["paused"] as? Bool ?? false
}

/// 記録のファイルを、ロックを取って読み、書き換える(listen.py と同じファイル)
func updateRecord(_ change: (inout [String: Any]) -> Void) {
    let fd = open(sessionsPath, O_RDWR | O_CREAT, 0o600)
    guard fd >= 0 else { return }
    defer { close(fd) }
    guard flock(fd, LOCK_EX) == 0 else { return }
    defer { flock(fd, LOCK_UN) }
    let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: false)
    let data = handle.readDataToEndOfFile()
    var root = (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
    change(&root)
    guard let out = try? JSONSerialization.data(withJSONObject: root) else { return }
    ftruncate(fd, 0)
    lseek(fd, 0, SEEK_SET)
    handle.write(out)
}

func setPaused(_ paused: Bool) {
    updateRecord { $0["paused"] = paused }
}

/// 話す相手を書く。nil は、相手を選ばない(「すべて」)
func setFocus(_ name: String?) {
    updateRecord { root in
        if let name = name { root["focus"] = name } else { root.removeValue(forKey: "focus") }
    }
}

/// 話す相手を読む。選んでいない、または、そのセッションが会話を終えていれば nil
func loadFocus() -> String? {
    guard let data = FileManager.default.contents(atPath: sessionsPath),
          let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let focus = root["focus"] as? String,
          let sessions = root["sessions"] as? [String: Any], sessions[focus] != nil else { return nil }
    return focus
}

/// セッションの記録を読む。読めなければ空を返す
func loadSessions() -> [String: SessionInfo] {
    guard let data = FileManager.default.contents(atPath: sessionsPath),
          let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let sessions = root["sessions"] as? [String: [String: Any]] else { return [:] }
    var out: [String: SessionInfo] = [:]
    for (name, v) in sessions {
        let lane = v["lane"] as? String ?? "normal"
        out[name] = SessionInfo(lane: lanes.contains { $0.key == lane } ? lane : "normal",
                                state: v["state"] as? String ?? "")
    }
    return out
}

/// Claude Code を終了したセッションを、記録から消す(タブからも消える)。
/// 記録にあるプロセス番号が、もう動いていないものが対象。番号のない記録は、そのままにする
func pruneEndedSessions() {
    guard let data = FileManager.default.contents(atPath: sessionsPath),
          let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let sessions = root["sessions"] as? [String: [String: Any]] else { return }
    func ended(_ v: [String: Any]) -> Bool {
        guard let pid = v["pid"] as? Int, pid > 0 else { return false }
        return kill(pid_t(pid), 0) != 0 && errno == ESRCH
    }
    guard sessions.values.contains(where: ended) else { return }
    updateRecord { root in
        var sessions = root["sessions"] as? [String: [String: Any]] ?? [:]
        for (name, v) in sessions where ended(v) {
            sessions.removeValue(forKey: name)
            if root["focus"] as? String == name { root.removeValue(forKey: "focus") }
        }
        root["sessions"] = sessions
    }
}

/// セッションの置き場所を書き換える
func setLane(_ name: String, _ lane: String) {
    updateRecord { root in
        var sessions = root["sessions"] as? [String: [String: Any]] ?? [:]
        var me = sessions[name] ?? ["state": "", "t": 0]
        me["lane"] = lane
        sessions[name] = me
        root["sessions"] = sessions
    }
}

/// 帯。空いているところをつかむと窓が動く。区画の枠と見出しを描く
final class DragBar: NSView {
    var zones: [(rect: NSRect, title: String)] = []
    var highlighted: Int? = nil
    var paused = false { didSet { needsDisplay = true } }
    override func mouseDown(with event: NSEvent) { window?.performDrag(with: event) }
    override func draw(_ dirtyRect: NSRect) {
        // 停止中は、帯を赤みがかった色にして、止まっていることが分かるようにする
        (paused ? NSColor(calibratedRed: 0.75, green: 0.20, blue: 0.20, alpha: 0.55)
                : NSColor.white.withAlphaComponent(0.10)).setFill()
        bounds.fill()
        for (i, z) in zones.enumerated() {
            let on = highlighted == i
            let path = NSBezierPath(roundedRect: z.rect, xRadius: 7, yRadius: 7)
            NSColor.white.withAlphaComponent(on ? 0.28 : 0.07).setFill()
            path.fill()
            NSColor.white.withAlphaComponent(on ? 0.9 : 0.22).setStroke()
            path.lineWidth = 1
            path.stroke()
            (z.title as NSString).draw(at: NSPoint(x: z.rect.minX + 7, y: z.rect.minY + 5), withAttributes: [
                .font: NSFont.systemFont(ofSize: 10, weight: .semibold),
                .foregroundColor: NSColor.white.withAlphaComponent(0.6),
            ])
        }
    }
}

/// セッションのタブ。押すと選び、つまんで動かすと置き場所を変える
final class TabChip: NSView {
    let name: String
    let draggable: Bool
    var selected = false { didSet { needsDisplay = true } }
    var state = "" { didSet { needsDisplay = true } }
    var onClick: ((String) -> Void)?
    var onDragMove: ((TabChip) -> Void)?
    var onDrop: ((TabChip) -> Void)?
    private var downPoint = NSPoint.zero
    private var downOrigin = NSPoint.zero
    private(set) var dragging = false
    static let font = NSFont.systemFont(ofSize: 12, weight: .medium)

    init(name: String, draggable: Bool) {
        self.name = name
        self.draggable = draggable
        let w = (name as NSString).size(withAttributes: [.font: TabChip.font]).width
        super.init(frame: NSRect(x: 0, y: 0, width: ceil(w) + (draggable ? 28 : 16), height: 20))
        toolTip = draggable
            ? "押すと、このセッションが話す相手になる(ほかのセッションは待つ)。つまんで、いま話す・あとで へ落とすと移る"
            : "押すと、相手を選ばない状態に戻る(いま話す のセッションが順番に話す)"
    }
    required init?(coder: NSCoder) { fatalError() }

    override func draw(_ dirtyRect: NSRect) {
        let path = NSBezierPath(roundedRect: bounds.insetBy(dx: 0.5, dy: 0.5), xRadius: 6, yRadius: 6)
        NSColor.white.withAlphaComponent(selected ? 0.30 : (dragging ? 0.22 : 0.12)).setFill()
        path.fill()
        var x: CGFloat = 8
        if draggable {
            let dot: NSColor
            switch state {
            case "talking": dot = NSColor(calibratedRed: 0.45, green: 0.90, blue: 0.55, alpha: 1)
            case "waiting": dot = NSColor(calibratedRed: 1.00, green: 0.62, blue: 0.25, alpha: 1)
            default: dot = NSColor.white.withAlphaComponent(0.25)
            }
            dot.setFill()
            NSBezierPath(ovalIn: NSRect(x: 7, y: 6.5, width: 7, height: 7)).fill()
            x = 20
        }
        (name as NSString).draw(at: NSPoint(x: x, y: 2.5), withAttributes: [
            .font: TabChip.font,
            .foregroundColor: selected ? NSColor.white : NSColor.white.withAlphaComponent(0.72),
        ])
    }

    override func mouseDown(with event: NSEvent) {
        downPoint = event.locationInWindow
        downOrigin = frame.origin
        dragging = false
    }
    override func mouseDragged(with event: NSEvent) {
        guard draggable else { return }
        let dx = event.locationInWindow.x - downPoint.x
        if !dragging && abs(dx) < 5 { return }
        dragging = true
        needsDisplay = true
        setFrameOrigin(NSPoint(x: downOrigin.x + dx, y: downOrigin.y))
        onDragMove?(self)
    }
    override func mouseUp(with event: NSEvent) {
        if dragging {
            dragging = false
            onDrop?(self)
        } else {
            onClick?(name)
        }
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
    var chips: [TabChip] = []
    var selected = allTab
    var lastStamp = ""
    var lastRaw: String? = nil
    var entries: [Entry] = []
    var sessions: [String: SessionInfo] = [:]
    var order: [String] = []       // タブに出すセッションの並び(現れた順)
    var paused = false
    let pauseButton = NSButton(frame: .zero)
    let helpButton = NSButton(frame: .zero)
    var showingHelp = false
    var draggingChip = false

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
        panel.minSize = NSSize(width: 420, height: 100)
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

        let close = NSButton(frame: NSRect(x: bar.bounds.width - 28, y: 5, width: 20, height: 20))
        close.title = "×"
        close.isBordered = false
        close.font = NSFont.systemFont(ofSize: 15, weight: .medium)
        close.contentTintColor = NSColor.white.withAlphaComponent(0.8)
        close.target = self
        close.action = #selector(hide(_:))
        close.autoresizingMask = [.minXMargin]
        close.toolTip = "字幕を隠す(次の読み上げや発言でまた出る)"
        bar.addSubview(close)

        pauseButton.frame = NSRect(x: bar.bounds.width - 104, y: 5, width: 70, height: 20)
        pauseButton.isBordered = false
        pauseButton.wantsLayer = true
        pauseButton.layer?.cornerRadius = 6
        pauseButton.font = NSFont.systemFont(ofSize: 12, weight: .semibold)
        pauseButton.target = self
        pauseButton.action = #selector(togglePause(_:))
        pauseButton.autoresizingMask = [.minXMargin]
        bar.addSubview(pauseButton)
        stylePause()

        helpButton.frame = NSRect(x: bar.bounds.width - 132, y: 5, width: 22, height: 20)
        helpButton.title = "?"
        helpButton.isBordered = false
        helpButton.wantsLayer = true
        helpButton.layer?.cornerRadius = 10
        helpButton.font = NSFont.systemFont(ofSize: 13, weight: .bold)
        helpButton.contentTintColor = .white
        helpButton.target = self
        helpButton.action = #selector(toggleHelp(_:))
        helpButton.autoresizingMask = [.minXMargin]
        helpButton.toolTip = "使い方を表示する。もう一度押すと、字幕に戻る"
        bar.addSubview(helpButton)
        styleHelp()
    }

    @objc func toggleHelp(_ sender: Any?) {
        showingHelp.toggle()
        styleHelp()
        render()
    }

    func styleHelp() {
        helpButton.layer?.backgroundColor = NSColor.white.withAlphaComponent(showingHelp ? 0.40 : 0.14).cgColor
    }

    /// 使い方の一覧を表示する
    func renderHelp() {
        let out = NSMutableAttributedString()
        let tabs = NSMutableParagraphStyle()
        tabs.tabStops = [NSTextTab(textAlignment: .left, location: 230)]
        tabs.headIndent = 230
        tabs.paragraphSpacing = 3
        for section in helpSections {
            let head = NSMutableParagraphStyle()
            head.paragraphSpacingBefore = 8
            head.paragraphSpacing = 4
            out.append(NSAttributedString(string: section.title + "\n", attributes: [
                .font: NSFont.systemFont(ofSize: 12, weight: .bold),
                .foregroundColor: claudeColor, .paragraphStyle: head,
            ]))
            for (key, value) in section.rows {
                out.append(NSAttributedString(string: key + "\t", attributes: [
                    .font: NSFont.systemFont(ofSize: 13, weight: .semibold),
                    .foregroundColor: userColor, .paragraphStyle: tabs,
                ]))
                out.append(NSAttributedString(string: value + "\n", attributes: [
                    .font: NSFont.systemFont(ofSize: 13, weight: .regular),
                    .foregroundColor: NSColor.white.withAlphaComponent(0.9), .paragraphStyle: tabs,
                ]))
            }
        }
        textView.textStorage?.setAttributedString(out)
        textView.scroll(NSPoint.zero)
    }

    @objc func togglePause(_ sender: Any?) {
        paused.toggle()
        setPaused(paused)
        stylePause()
    }

    func stylePause() {
        pauseButton.title = paused ? "▶ 再開" : "⏸ 停止"
        pauseButton.contentTintColor = .white
        pauseButton.layer?.backgroundColor = NSColor.white.withAlphaComponent(paused ? 0.35 : 0.14).cgColor
        pauseButton.toolTip = paused
            ? "押すと、音声の会話を再開する"
            : "押すと、すべてのセッションが話すのも聞くのもやめる(会議のときなど)"
        bar.paused = paused
    }

    @objc func hide(_ sender: Any?) { panel.orderOut(nil) }

    func parse(_ raw: String) -> [Entry] {
        raw.split(separator: "\n", omittingEmptySubsequences: true).compactMap { line in
            let f = line.split(separator: "\t", maxSplits: 3, omittingEmptySubsequences: false).map(String.init)
            return f.count == 4 ? Entry(time: f[0], session: f[1], who: f[2], text: f[3]) : nil
        }
    }

    func lane(of name: String) -> String { sessions[name]?.lane ?? "normal" }

    /// 帯に、「すべて」のタブと、2 つの区画、その中のセッションのタブを並べる
    func layoutBar() {
        chips.forEach { $0.removeFromSuperview() }
        chips = []
        var x: CGFloat = 8
        let all = TabChip(name: allTab, draggable: false)
        all.onClick = { [weak self] _ in self?.select(allTab) }
        all.setFrameOrigin(NSPoint(x: x, y: 5))
        bar.addSubview(all)
        chips.append(all)
        x += all.frame.width + 8

        var zones: [(rect: NSRect, title: String)] = []
        for l in lanes {
            let titleWidth = ceil((l.title as NSString).size(withAttributes: [
                .font: NSFont.systemFont(ofSize: 10, weight: .semibold)]).width)
            let zoneX = x
            x += 7 + titleWidth + 7
            for name in order where lane(of: name) == l.key {
                let chip = TabChip(name: name, draggable: true)
                chip.state = sessions[name]?.state ?? ""
                chip.onClick = { [weak self] n in self?.select(n) }
                chip.onDragMove = { [weak self] c in self?.dragMoved(c) }
                chip.onDrop = { [weak self] c in self?.dropped(c) }
                chip.setFrameOrigin(NSPoint(x: x, y: 5))
                bar.addSubview(chip)
                chips.append(chip)
                x += chip.frame.width + 4
            }
            x += 22  // 空の区画にも、落とせる余白を残す
            zones.append((NSRect(x: zoneX, y: 2, width: x - zoneX, height: barHeight - 4), l.title))
            x += 6
        }
        bar.zones = zones
        bar.highlighted = nil
        bar.needsDisplay = true
        styleChips()
    }

    func styleChips() {
        for c in chips { c.selected = c.name == selected }
    }

    /// タブを押した。そのセッションを話す相手にする(「すべて」なら、相手を選ばない状態に戻す)
    func select(_ name: String) {
        if showingHelp {  // 使い方を出していたら、字幕に戻す
            showingHelp = false
            styleHelp()
        }
        selected = name
        setFocus(name == allTab ? nil : name)
        styleChips()
        render()
    }

    func zoneIndex(at x: CGFloat) -> Int? {
        if let i = bar.zones.firstIndex(where: { x >= $0.rect.minX && x <= $0.rect.maxX }) { return i }
        guard let first = bar.zones.first, let last = bar.zones.last else { return nil }
        return x < first.rect.minX ? 0 : (x > last.rect.maxX ? bar.zones.count - 1 : nil)
    }

    func dragMoved(_ chip: TabChip) {
        draggingChip = true
        bar.highlighted = zoneIndex(at: chip.frame.midX)
        bar.needsDisplay = true
    }

    func dropped(_ chip: TabChip) {
        draggingChip = false
        if let i = zoneIndex(at: chip.frame.midX), lanes[i].key != lane(of: chip.name) {
            setLane(chip.name, lanes[i].key)
            sessions = loadSessions()
            // 話す相手に選んでいるセッションを「あとで」へ移したら、相手を選ばない状態に戻す
            if lanes[i].key == "later" && selected == chip.name {
                selected = allTab
                setFocus(nil)
                render()
            }
        }
        layoutBar()
    }

    func render() {
        if showingHelp {
            renderHelp()
            return
        }
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
            if selected == allTab { head += "  [\(e.session)]" }
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

    /// 字幕のファイルを読む。新しい発言があれば true を返す
    func readCaptions() -> Bool {
        let fm = FileManager.default
        let names = ((try? fm.contentsOfDirectory(atPath: captionDir)) ?? []).filter { $0.hasSuffix(".txt") }.sorted()
        guard let name = names.last else {
            if lastRaw == nil { lastRaw = "" }
            return false
        }
        let path = captionDir + "/" + name
        let attrs = try? fm.attributesOfItem(atPath: path)
        let stamp = "\(name) \(attrs?[.modificationDate] ?? "") \(attrs?[.size] ?? "")"
        if stamp == lastStamp { return false }
        lastStamp = stamp
        let raw = (try? String(contentsOfFile: path, encoding: .utf8)) ?? ""
        if raw == lastRaw { return false }
        lastRaw = raw
        entries = Array(parse(raw).suffix(maxShownEntries))
        return true
    }

    var ticks = 0

    func refresh() {
        ticks += 1
        if ticks % 10 == 1 { pruneEndedSessions() }  // 約 3 秒ごとに、終了したセッションを消す
        let first = lastRaw == nil
        let newCaption = readCaptions()
        let newSessions = loadSessions()
        let sessionsChanged = newSessions != sessions
        // 選んでいるタブは、記録にある「話す相手」に合わせる(窓を出し直しても引き継ぐ)
        let focus = loadFocus() ?? allTab
        let focusChanged = focus != selected
        if !newCaption && !sessionsChanged && !focusChanged && !first { return }
        sessions = newSessions

        let nowPaused = loadPaused()
        if nowPaused != paused {
            paused = nowPaused
            stylePause()
        }

        // タブに出すセッション: 記録にあるもの(音声の会話をしているセッション)。
        // 会話を終えたセッションは記録から消えるので、タブからも消える。字幕は「すべて」で読める
        var names = order.filter { sessions[$0] != nil }
        for n in sessions.keys.sorted() where !names.contains(n) { names.append(n) }
        let orderChanged = names != order
        order = names
        selected = order.contains(focus) ? focus : allTab
        if !draggingChip && (sessionsChanged || orderChanged || first) { layoutBar() } else { styleChips() }
        // 使い方を表示している間は、描き直さない(読んでいる位置が先頭に戻らないようにする)
        if (newCaption || focusChanged || first) && !showingHelp { render() }
        if newCaption && !entries.isEmpty && (!panel.isVisible || first) { panel.orderFrontRegardless() }
        if first && !entries.isEmpty { panel.orderFrontRegardless() }
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

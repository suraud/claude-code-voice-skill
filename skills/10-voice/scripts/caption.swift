// 字幕の窓: 字幕のファイルの内容を、画面に浮かぶ小さな窓へ表示する。
//
// 使い方: caption <字幕のファイル>
// - どのアプリの上にも浮かび、デスクトップを切り替えてもついてくる
// - 上の帯をつかむと動かせる。端をつかむと大きさを変えられる
// - 本文はスクロールでさかのぼれる。新しい行が来ると、いちばん下へ戻る
// - 右上の × で隠せる。隠しても、次の読み上げや発言でまた出る
// - 表示するだけで、どこにも送信しない

import AppKit

let POLL_S: TimeInterval = 0.3

guard CommandLine.arguments.count > 1 else {
    FileHandle.standardError.write("使い方: caption <字幕のファイル>\n".data(using: .utf8)!)
    exit(2)
}
let path = CommandLine.arguments[1]

/// つかんで窓を動かすための帯
final class DragBar: NSView {
    override func mouseDown(with event: NSEvent) { window?.performDrag(with: event) }
    override func draw(_ dirtyRect: NSRect) {
        NSColor.white.withAlphaComponent(0.10).setFill()
        bounds.fill()
        let title = "字幕(ここをつかんで動かす)" as NSString
        title.draw(at: NSPoint(x: 12, y: 3), withAttributes: [
            .font: NSFont.systemFont(ofSize: 11),
            .foregroundColor: NSColor.white.withAlphaComponent(0.55),
        ])
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)  // Dock に出さず、ほかのアプリの操作を奪わない

let screen = (NSScreen.main ?? NSScreen.screens[0]).visibleFrame
let width = min(900, screen.width * 0.6)
let height: CGFloat = 190
let barHeight: CGFloat = 22
let frame = NSRect(x: screen.midX - width / 2, y: screen.minY + 40, width: width, height: height)

let panel = NSPanel(contentRect: frame, styleMask: [.nonactivatingPanel, .borderless, .resizable],
                    backing: .buffered, defer: false)
panel.level = .floating
panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
panel.isOpaque = false
panel.backgroundColor = NSColor.black.withAlphaComponent(0.80)
panel.hasShadow = true
panel.hidesOnDeactivate = false
panel.minSize = NSSize(width: 320, height: 90)
panel.setFrameAutosaveName("10-voice-caption")  // 動かした位置と大きさを覚える

let content = panel.contentView!
content.wantsLayer = true
content.layer?.cornerRadius = 12
content.layer?.masksToBounds = true

let bar = DragBar(frame: NSRect(x: 0, y: content.bounds.height - barHeight, width: content.bounds.width, height: barHeight))
bar.autoresizingMask = [.width, .minYMargin]
content.addSubview(bar)

/// × ボタン。窓を隠すだけで、終了はしない
final class CloseTarget: NSObject {
    @objc func hide(_ sender: Any?) { (sender as? NSView)?.window?.orderOut(nil) }
}
let closeTarget = CloseTarget()
let closeButton = NSButton(frame: NSRect(x: bar.bounds.width - 26, y: 1, width: 20, height: 20))
closeButton.title = "×"
closeButton.isBordered = false
closeButton.font = NSFont.systemFont(ofSize: 15, weight: .medium)
closeButton.contentTintColor = NSColor.white.withAlphaComponent(0.8)
closeButton.target = closeTarget
closeButton.action = #selector(CloseTarget.hide(_:))
closeButton.autoresizingMask = [.minXMargin]
closeButton.toolTip = "字幕を隠す(次の読み上げや発言でまた出る)"
bar.addSubview(closeButton)

let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: content.bounds.width, height: content.bounds.height - barHeight))
scroll.autoresizingMask = [.width, .height]
scroll.hasVerticalScroller = true
scroll.drawsBackground = false
let textView = NSTextView(frame: scroll.bounds)
textView.isEditable = false
textView.isSelectable = true
textView.drawsBackground = false
textView.textColor = .white
textView.font = NSFont.systemFont(ofSize: 16)
textView.textContainerInset = NSSize(width: 12, height: 8)
textView.autoresizingMask = [.width]
textView.isVerticallyResizable = true
textView.textContainer?.widthTracksTextView = true
scroll.documentView = textView
content.addSubview(scroll)

var lastText: String? = nil

func refresh() {
    let text = (try? String(contentsOfFile: path, encoding: .utf8)) ?? ""
    if text != lastText {
        lastText = text
        let style = NSMutableParagraphStyle()
        style.paragraphSpacing = 6
        style.lineSpacing = 2
        textView.textStorage?.setAttributedString(NSAttributedString(string: text, attributes: [
            .font: NSFont.systemFont(ofSize: 16),
            .foregroundColor: NSColor.white,
            .paragraphStyle: style,
        ]))
        textView.scrollToEndOfDocument(nil)
        if !text.isEmpty && !panel.isVisible { panel.orderFrontRegardless() }
    }
}

refresh()
if !(lastText ?? "").isEmpty { panel.orderFrontRegardless() }
Timer.scheduledTimer(withTimeInterval: POLL_S, repeats: true) { _ in refresh() }
signal(SIGTERM) { _ in exit(0) }
app.run()

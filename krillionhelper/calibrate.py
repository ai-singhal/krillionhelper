"""Pick the screen region that holds the prompt text.

A small control window opens first, with the overlay hidden, so there is time to get the
game ready. Tab (or the buttons) shows and hides the dimmed overlay; drag a box on it to
finish. Esc, Cancel or Cmd+Q cancels. The overlay has its own clickable buttons because
macOS doesn't send keystrokes to borderless windows.
"""

import sys

import mss

MIN_SIZE = 12
IDLE_TEXT = (
    "1. Get the game's prompt card on screen.\n"
    "2. Press Tab (or click Select region) to show the overlay.\n"
    "3. Drag a box around the prompt card.\n\n"
    "Tab hides the overlay again. Esc cancels."
)
SHOWN_TEXT = "Drag a box around the prompt card.\nTab or Hide overlay hides it. Esc cancels."


def activateApp():
    """Bring this Python app to the front so Tab and Esc reach it (macOS)."""
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication

        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    except Exception:
        pass


def selectRegion():
    """Returns {"left", "top", "width", "height"} in global screen points, or None if cancelled."""
    import tkinter as tk

    with mss.MSS() as sct:
        monitors = sct.monitors[1:]

    root = tk.Tk()
    root.title("Trivia helper: select the prompt region")
    root.attributes("-topmost", True)
    root.resizable(False, False)
    result = {}
    state = {"start": None, "shown": False}
    overlays = []  # (window, canvas, rubber-band rectangle)

    def finish(region):
        result["region"] = region
        root.quit()

    def cancel(_event=None):
        finish(None)
        return "break"

    def showOverlay():
        for win, canvas, rect in overlays:
            canvas.coords(rect, 0, 0, 0, 0)
            win.deiconify()
            win.lift()
        state["shown"] = True
        status.config(text=SHOWN_TEXT)
        toggleButton.config(text="Hide overlay (Tab)")
        activateApp()
        root.focus_force()

    def hideOverlay():
        for win, _, _ in overlays:
            win.withdraw()
        state["shown"] = False
        state["start"] = None
        status.config(text=IDLE_TEXT)
        toggleButton.config(text="Select region (Tab)")

    def toggle(_event=None):
        hideOverlay() if state["shown"] else showOverlay()
        return "break"

    def onButton(canvas):
        item = canvas.find_withtag("current")
        return bool(item) and "button" in canvas.gettags(item[0])

    def onPress(event):
        if not onButton(event.widget):
            state["start"] = (event.x_root, event.y_root)

    def onMotion(event):
        if not state["start"]:
            return
        x0, y0 = state["start"]
        for win, canvas, rect in overlays:
            ox, oy = win.winfo_rootx(), win.winfo_rooty()
            canvas.coords(rect, x0 - ox, y0 - oy, event.x_root - ox, event.y_root - oy)

    def onRelease(event):
        if not state["start"]:
            return
        x0, y0 = state["start"]
        x1, y1 = event.x_root, event.y_root
        state["start"] = None
        width, height = abs(x1 - x0), abs(y1 - y0)
        if width < MIN_SIZE or height < MIN_SIZE:
            return
        finish({"left": int(min(x0, x1)), "top": int(min(y0, y1)), "width": int(width), "height": int(height)})

    # Control window: always reachable, overlay starts hidden.
    frame = tk.Frame(root, padx=20, pady=16)
    frame.pack()
    status = tk.Label(frame, text=IDLE_TEXT, justify="left", font=("Helvetica", 15))
    status.pack(anchor="w")
    buttons = tk.Frame(frame, pady=12)
    buttons.pack(anchor="w")
    toggleButton = tk.Button(buttons, text="Select region (Tab)", command=toggle, width=18, takefocus=0)
    toggleButton.pack(side="left")
    tk.Button(buttons, text="Cancel (Esc)", command=cancel, takefocus=0).pack(side="left", padx=(10, 0))

    for mon in monitors:
        win = tk.Toplevel(root)
        win.overrideredirect(True)
        # macOS Tk ignores a borderless window's position until it has been drawn once.
        win.update()
        win.overrideredirect(True)
        win.geometry(f"{mon['width']}x{mon['height']}+{mon['left']}+{mon['top']}")
        win.attributes("-topmost", True)
        win.attributes("-alpha", 0.35)
        canvas = tk.Canvas(win, bg="black", highlightthickness=0, cursor="crosshair")
        canvas.pack(fill="both", expand=True)
        mid = mon["width"] // 2
        canvas.create_text(mid, 70, text="Drag a box around the game's prompt card", fill="white",
                           font=("Helvetica", 24, "bold"))
        canvas.create_text(mid - 110, 120, text="[ Hide overlay ]", fill="#00e5ff",
                           font=("Helvetica", 20, "bold"), tags=("button", "hide"))
        canvas.create_text(mid + 110, 120, text="[ Cancel ]", fill="#ff8fa3",
                           font=("Helvetica", 20, "bold"), tags=("button", "cancel"))
        canvas.tag_bind("hide", "<ButtonRelease-1>", toggle)
        canvas.tag_bind("cancel", "<ButtonRelease-1>", cancel)
        rect = canvas.create_rectangle(0, 0, 0, 0, outline="#00e5ff", width=3)
        canvas.bind("<ButtonPress-1>", onPress)
        canvas.bind("<B1-Motion>", onMotion)
        canvas.bind("<ButtonRelease-1>", onRelease)
        win.withdraw()
        overlays.append((win, canvas, rect))

    # Bind on the toplevel (not "all"): Tk's focus traversal grabs Tab before an "all" binding.
    for widget in [root] + [w for w, _, _ in overlays]:
        widget.bind("<Tab>", toggle)
        widget.bind("<Escape>", cancel)
    root.protocol("WM_DELETE_WINDOW", cancel)
    if sys.platform == "darwin":
        root.createcommand("tk::mac::Quit", cancel)  # Cmd+Q cancels instead of killing the process
    root.after(200, activateApp)
    root.mainloop()
    root.destroy()
    return result.get("region")

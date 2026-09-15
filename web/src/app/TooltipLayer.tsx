import { useEffect, useId } from "react";

/** One tooltip surface outside the dock's scroll/clipping boundaries.
 * Annotated controls keep their native semantics and layout. Position is
 * applied through CSSOM (as in Dockview), compatible with style-src 'self'. */
export function TooltipLayer() {
  const id = useId();
  useEffect(() => {
    let anchor: HTMLElement | null = null;
    let tip: HTMLDivElement | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const observer = new MutationObserver(() => {
      if (anchor && !anchor.isConnected) hide();
    });

    function cancel() { clearTimeout(timer); }
    function hide() {
      cancel();
      observer.disconnect();
      if (anchor) {
        const remaining = (anchor.getAttribute("aria-describedby") ?? "").split(/\s+/).filter((value) => value && value !== id);
        if (remaining.length) anchor.setAttribute("aria-describedby", remaining.join(" "));
        else anchor.removeAttribute("aria-describedby");
      }
      tip?.remove();
      tip = null;
      anchor = null;
    }
    function show(target: HTMLElement) {
      cancel();
      if (target === anchor) return;
      hide();
      const text = target.dataset.tooltip;
      if (!text || !target.isConnected) return;
      anchor = target;
      tip = document.createElement("div");
      tip.id = id;
      tip.role = "tooltip";
      tip.className = "terminal-tooltip";
      tip.textContent = text;
      document.body.append(tip);
      const box = target.getBoundingClientRect();
      const width = tip.offsetWidth;
      const height = tip.offsetHeight;
      const left = Math.max(8, Math.min(box.left, window.innerWidth - width - 8));
      const top = box.bottom + height + 8 > window.innerHeight ? box.top - height - 6 : box.bottom + 6;
      tip.style.left = `${left}px`;
      tip.style.top = `${Math.max(8, top)}px`;
      const existing = target.getAttribute("aria-describedby");
      target.setAttribute("aria-describedby", [existing, id].filter(Boolean).join(" "));
      observer.observe(document.body, { childList: true, subtree: true });
    }
    function control(target: EventTarget | null) {
      return target instanceof Element ? target.closest<HTMLElement>("[data-tooltip]") : null;
    }
    function inside(target: EventTarget | null) {
      return target instanceof Node && (anchor?.contains(target) || tip?.contains(target));
    }
    function enter(event: PointerEvent) {
      if (event.pointerType === "touch") return;
      if (inside(event.target)) { cancel(); return; }
      const target = control(event.target);
      if (target) { hide(); timer = setTimeout(() => show(target), 250); }
    }
    function leave(event: PointerEvent) {
      if (inside(event.relatedTarget)) return;
      cancel();
      if (anchor?.contains(document.activeElement)) return;
      timer = setTimeout(hide, 120);
    }
    function focus(event: FocusEvent) {
      const target = control(event.target);
      if (target) show(target);
      else hide();
    }
    function blur(event: FocusEvent) {
      if (!inside(event.relatedTarget)) hide();
    }
    function key(event: KeyboardEvent) {
      if (event.key === "Escape" && tip) {
        event.preventDefault();
        event.stopPropagation();
        hide();
      }
    }
    document.addEventListener("pointerover", enter);
    document.addEventListener("pointerout", leave);
    document.addEventListener("focusin", focus);
    document.addEventListener("focusout", blur);
    document.addEventListener("keydown", key, true);
    document.addEventListener("pointerdown", hide, true);
    document.addEventListener("click", hide, true);
    document.addEventListener("scroll", hide, true);
    window.addEventListener("resize", hide);
    window.addEventListener("blur", hide);
    return () => {
      hide();
      document.removeEventListener("pointerover", enter);
      document.removeEventListener("pointerout", leave);
      document.removeEventListener("focusin", focus);
      document.removeEventListener("focusout", blur);
      document.removeEventListener("keydown", key, true);
      document.removeEventListener("pointerdown", hide, true);
      document.removeEventListener("click", hide, true);
      document.removeEventListener("scroll", hide, true);
      window.removeEventListener("resize", hide);
      window.removeEventListener("blur", hide);
    };
  }, [id]);
  return null;
}

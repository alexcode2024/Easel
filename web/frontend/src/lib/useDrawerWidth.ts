// Resizable width for the right-hand drawers (content library preview + skill
// drawer). One shared preference: resizing either drawer resizes both, and the
// value survives reloads.
//
// The narrow strip lives on the drawer's LEFT edge because the drawer is pinned
// to the right of its overlay.

import { useCallback, useEffect, useRef, useState } from 'react';

const STORAGE_KEY = 'easel_drawer_width';

/** Matches the historical hard-coded `.drawer { width: 560px }`. */
export const DEFAULT_DRAWER_WIDTH = 560;
const MIN_DRAWER_WIDTH = 360;

/** `max-width: 92vw` in CSS, so a restored width never exceeds the viewport. */
function maxWidth(): number {
  if (typeof window === 'undefined') return DEFAULT_DRAWER_WIDTH;
  return Math.max(MIN_DRAWER_WIDTH, Math.round(window.innerWidth * 0.92));
}

function clamp(value: number): number {
  return Math.min(Math.max(Math.round(value), MIN_DRAWER_WIDTH), maxWidth());
}

function restore(): number {
  if (typeof window === 'undefined') return DEFAULT_DRAWER_WIDTH;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    const parsed = raw === null ? NaN : Number.parseInt(raw, 10);
    return Number.isFinite(parsed) ? clamp(parsed) : DEFAULT_DRAWER_WIDTH;
  } catch {
    return DEFAULT_DRAWER_WIDTH;
  }
}

/**
 * Returns the persisted width plus bindings to spread onto the drawer element
 * (`style`) and the resize strip (`handleProps`).
 *
 * `handleProps` uses function refs instead of a DOM lookup so the hook has no
 * dependency on class names.
 */
export function useDrawerWidth() {
  const [width, setWidth] = useState<number>(restore);
  // Mirrors `width` for the stable ref callback below. Reading state directly
  // there would force `width` into that callback's deps, and a changing ref
  // identity makes React detach + reattach the node on every pointermove —
  // which fires the active drag's cleanup and kills the gesture after one frame.
  const widthRef = useRef(width);
  widthRef.current = width;
  const cleanupRef = useRef<(() => void) | null>(null);

  // Keep the value legal after a window resize (92vw shrinks with the window).
  useEffect(() => {
    const onResize = () => setWidth((w) => clamp(w));
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  useEffect(() => () => cleanupRef.current?.(), []);

  useEffect(() => {
    try {
      window.localStorage.setItem(STORAGE_KEY, String(width));
    } catch {
      /* private mode / quota — the width simply will not persist */
    }
  }, [width]);

  const onHandleRef = useCallback((node: HTMLDivElement | null) => {
    // Re-attaching on every render would drop an in-flight drag; only rewrite
    // the ref callback when the node identity actually changes.
    cleanupRef.current?.();
    cleanupRef.current = null;
    if (!node) return;

    // While dragging, listen on `window` so the pointer may leave the 6px strip
    // (fast drags, or leaving the window) without ending the gesture.
    const onPointerDown = (event: PointerEvent) => {
      event.preventDefault();
      const startX = event.clientX;
      const startWidth =
        node.parentElement?.getBoundingClientRect().width ?? widthRef.current;
      const prevCursor = document.body.style.cursor;
      const prevSelect = document.body.style.userSelect;
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';

      const onMove = (move: PointerEvent) => {
        setWidth(clamp(startWidth - (move.clientX - startX)));
      };
      const onUp = () => {
        window.removeEventListener('pointermove', onMove);
        window.removeEventListener('pointerup', onUp);
        window.removeEventListener('pointercancel', onUp);
        document.body.style.cursor = prevCursor;
        document.body.style.userSelect = prevSelect;
      };
      window.addEventListener('pointermove', onMove);
      window.addEventListener('pointerup', onUp);
      window.addEventListener('pointercancel', onUp);
      cleanupRef.current = onUp;
    };

    const onKeyDown = (event: KeyboardEvent) => {
      const step = event.shiftKey ? 50 : 20;
      if (event.key === 'ArrowLeft') {
        event.preventDefault();
        setWidth((w) => clamp(w + step));
      } else if (event.key === 'ArrowRight') {
        event.preventDefault();
        setWidth((w) => clamp(w - step));
      } else if (event.key === 'Home') {
        event.preventDefault();
        setWidth(DEFAULT_DRAWER_WIDTH);
      }
    };

    node.addEventListener('pointerdown', onPointerDown);
    node.addEventListener('keydown', onKeyDown);
    cleanupRef.current = () => {
      node.removeEventListener('pointerdown', onPointerDown);
      node.removeEventListener('keydown', onKeyDown);
    };
    // Intentionally empty deps: a stable ref identity keeps the node attached
    // across renders so an in-flight drag survives. `width` is read via ref.
  }, []);

  const onReset = useCallback(() => setWidth(DEFAULT_DRAWER_WIDTH), []);

  return {
    width,
    /** Spread onto the `.drawer` element. */
    style: { '--drawer-w': `${width}px` } as React.CSSProperties,
    /** Spread onto the `.drawer-resizer` element. */
    handleProps: {
      ref: onHandleRef,
      role: 'separator' as const,
      'aria-orientation': 'vertical' as const,
      'aria-label': '调整面板宽度',
      'aria-valuenow': width,
      'aria-valuemin': MIN_DRAWER_WIDTH,
      'aria-valuemax': maxWidth(),
      tabIndex: 0,
      title: '拖动调整宽度（双击复位，或聚焦后用 ←/→）',
      onDoubleClick: onReset,
    },
  };
}

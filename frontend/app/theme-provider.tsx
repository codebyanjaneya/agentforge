"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";

export type Theme = "dark" | "light";

const STORAGE_KEY = "agentforge-theme";

interface ThemeContextValue {
  theme: Theme;
  toggle: () => void;
  setTheme: (theme: Theme) => void;
}

const ThemeContext = createContext<ThemeContextValue>({
  theme: "dark",
  toggle: () => {},
  setTheme: () => {},
});

export function ThemeProvider({ children }: { children: ReactNode }) {
  // Server + first client render are deterministically "dark" to avoid a
  // hydration mismatch; the real preference is applied in the effect below.
  const [theme, setThemeState] = useState<Theme>("dark");

  useEffect(() => {
    let stored: Theme | null = null;
    try {
      stored = localStorage.getItem(STORAGE_KEY) as Theme | null;
    } catch {
      // localStorage unavailable (private mode, SSR edge cases)
    }
    const initial =
      stored ??
      (document.documentElement.getAttribute("data-theme") as Theme | null) ??
      "dark";
    // The DOM attribute is an external system and can be set synchronously;
    // defer the React state update to avoid a cascading render in the effect.
    document.documentElement.setAttribute("data-theme", initial);
    const id = requestAnimationFrame(() => setThemeState(initial));
    return () => cancelAnimationFrame(id);
  }, []);

  const setTheme = useCallback((next: Theme) => {
    setThemeState(next);
    document.documentElement.setAttribute("data-theme", next);
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      // ignore persistence failures
    }
  }, []);

  const toggle = useCallback(() => {
    setThemeState((prev) => {
      const next: Theme = prev === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      try {
        localStorage.setItem(STORAGE_KEY, next);
      } catch {
        // ignore persistence failures
      }
      return next;
    });
  }, []);

  return (
    <ThemeContext.Provider value={{ theme, toggle, setTheme }}>
      {children}
    </ThemeContext.Provider>
  );
}

export function useTheme() {
  return useContext(ThemeContext);
}

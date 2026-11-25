"use client";

// The command palette: Ctrl/Cmd+K (or the Search button) opens a list of places and actions; type to filter, arrows to move, Enter to go, Escape to close.
// Accessibility: a native modal <dialog> (focus is trapped, the page behind is inert, Escape closes it and focus returns to what had it), the ARIA combobox + listbox
// pattern with aria-activedescendant (focus stays in the input while the screen reader follows the highlighted option), and a polite status that says how many results
// there are. No dependency, no network: every entry is a link or an action that already exists.

import { Activity, ArrowRight, LayoutTemplate, ListChecks, Moon, Plus, Search, Settings, ShieldCheck, Sun, UserPlus, Users, Wrench, Gauge, type LucideIcon } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useId, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { useTheme } from "../theme-provider";

const noSubscribe = () => () => {};
const isMac = () => /Mac|iPhone|iPad/.test(navigator.platform);

type Command = { id: string; group: "Go to" | "Create" | "Appearance"; label: string; hint?: string; keywords?: string; Icon: LucideIcon; run: () => void };

export function CommandPalette() {
  const router = useRouter();
  const { theme, toggle } = useTheme();
  const dialog = useRef<HTMLDialogElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const mac = useSyncExternalStore(noSubscribe, isMac, () => false); // the server and the first paint say "Ctrl K"; the real platform is read after hydration
  const base = useId();

  const go = (href: string) => () => router.push(href);
  const commands = useMemo<Command[]>(
    () => [
      { id: "overview", group: "Go to", label: "Overview", Icon: Gauge, run: go("/studio") },
      { id: "plans", group: "Go to", label: "Plans", keywords: "objectives drafts", Icon: ListChecks, run: go("/studio/plans") },
      { id: "runs", group: "Go to", label: "Runs", keywords: "execution history", Icon: Activity, run: go("/studio/runs") },
      { id: "approvals", group: "Go to", label: "Approvals", keywords: "decide waiting inbox", Icon: ShieldCheck, run: go("/studio/approvals") },
      { id: "agents", group: "Go to", label: "Agents", keywords: "workforce specialists", Icon: Users, run: go("/studio/agents") },
      { id: "tools", group: "Go to", label: "Tools", keywords: "registry", Icon: Wrench, run: go("/studio/tools") },
      { id: "activity", group: "Go to", label: "Tool activity", keywords: "dispatches calls history", Icon: Activity, run: go("/studio/tools/activity") },
      { id: "templates", group: "Go to", label: "Templates", keywords: "agent templates", Icon: LayoutTemplate, run: go("/studio/templates") },
      { id: "team", group: "Go to", label: "Team", keywords: "members invitations workspace", Icon: UserPlus, run: go("/studio/team") },
      { id: "settings", group: "Go to", label: "Settings", keywords: "workspace roles", Icon: Settings, run: go("/studio/settings") },
      { id: "classic", group: "Go to", label: "Classic dashboard", keywords: "legacy", Icon: ArrowRight, run: go("/classic") },
      {
        id: "objective",
        group: "Create",
        label: "New objective",
        hint: "Write what you want done",
        keywords: "delegate plan task",
        Icon: Plus,
        run: () => {
          router.push("/studio#objective");
          setTimeout(() => document.getElementById("objective")?.focus(), 250);
        },
      },
      { id: "new-agent", group: "Create", label: "Create an agent", hint: "From scratch", keywords: "new agent specialist", Icon: Users, run: go("/studio/agents/new") },
      { id: "from-template", group: "Create", label: "Add an agent from a template", keywords: "starter", Icon: LayoutTemplate, run: go("/studio/templates") },
      { id: "theme", group: "Appearance", label: theme === "dark" ? "Switch to the light theme" : "Switch to the dark theme", keywords: "dark light mode appearance", Icon: theme === "dark" ? Sun : Moon, run: toggle },
    ],
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [theme, toggle, router],
  );

  const results = useMemo(() => {
    const words = query.toLowerCase().split(/\s+/).filter(Boolean);
    const match = commands.filter((c) => words.every((w) => `${c.label} ${c.group} ${c.keywords ?? ""} ${c.hint ?? ""}`.toLowerCase().includes(w)));
    const first = words[0] ?? "";
    return first ? [...match].sort((a, b) => Number(b.label.toLowerCase().startsWith(first)) - Number(a.label.toLowerCase().startsWith(first))) : match;
  }, [commands, query]);
  const groups = useMemo(() => (["Go to", "Create", "Appearance"] as const).map((name) => ({ name, items: results.filter((r) => r.group === name) })).filter((g) => g.items.length), [results]);
  const flat = groups.flatMap((g) => g.items);
  const current = Math.min(active, Math.max(flat.length - 1, 0));

  function show() {
    setQuery("");
    setActive(0);
    setOpen(true);
    if (dialog.current && !dialog.current.open) dialog.current.showModal();
    requestAnimationFrame(() => input.current?.focus());
  }
  function hide() {
    setOpen(false);
    if (dialog.current?.open) dialog.current.close();
  }

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === "k") {
        event.preventDefault();
        if (dialog.current?.open) hide();
        else show();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  function choose(command: Command | undefined) {
    if (!command) return;
    hide();
    command.run();
  }
  function onInputKey(event: React.KeyboardEvent) {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActive(flat.length ? (current + 1) % flat.length : 0);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive(flat.length ? (current - 1 + flat.length) % flat.length : 0);
    } else if (event.key === "Home") {
      event.preventDefault();
      setActive(0);
    } else if (event.key === "End") {
      event.preventDefault();
      setActive(Math.max(flat.length - 1, 0));
    } else if (event.key === "Enter") {
      event.preventDefault();
      choose(flat[current]);
    }
  }

  useEffect(() => {
    if (open) document.getElementById(`${base}-opt-${flat[current]?.id}`)?.scrollIntoView({ block: "nearest" });
  }, [open, current, flat, base]);

  return (
    <>
      <button type="button" className="st-search" onClick={show} aria-label="Search pages and actions" aria-haspopup="dialog" aria-keyshortcuts="Control+K Meta+K">
        <Search size={15} aria-hidden="true" />
        <span className="st-search-text">Search</span>
        <kbd className="st-kbd" aria-hidden="true">
          {mac ? "⌘ K" : "Ctrl K"}
        </kbd>
      </button>
      <dialog
        ref={dialog}
        className="st-palette"
        aria-label="Command palette"
        onClose={() => setOpen(false)}
        onClick={(event) => {
          if (event.target === dialog.current) hide(); // a click on the backdrop closes it
        }}
      >
        <div className="st-palette-input-row">
          <Search size={17} aria-hidden="true" />
          <input
            ref={input}
            className="st-palette-input"
            type="text"
            role="combobox"
            aria-expanded={flat.length > 0}
            aria-controls={flat.length ? `${base}-list` : undefined} /* the listbox only exists while there are results */
            aria-activedescendant={flat[current] ? `${base}-opt-${flat[current].id}` : undefined}
            aria-autocomplete="list"
            aria-label="Search pages and actions"
            placeholder="Go to a page or start something…"
            autoComplete="off"
            spellCheck={false}
            value={query}
            onChange={(event) => {
              setQuery(event.target.value);
              setActive(0);
            }}
            onKeyDown={onInputKey}
          />
        </div>
        <div className="sr-live" role="status" aria-live="polite">
          {open ? (flat.length ? `${flat.length} result${flat.length === 1 ? "" : "s"}` : "No results") : ""}
        </div>
        {flat.length ? (
          <ul className="st-palette-list" role="listbox" id={`${base}-list`} aria-label="Results">
            {groups.map((group) => (
              <li key={group.name} role="presentation">
                <div className="st-palette-group" aria-hidden="true">
                  {group.name}
                </div>
                <ul role="group" aria-label={group.name} style={{ listStyle: "none", margin: 0, padding: 0 }}>
                  {group.items.map((command) => {
                    const selected = flat[current]?.id === command.id;
                    return (
                      <li
                        key={command.id}
                        id={`${base}-opt-${command.id}`}
                        role="option"
                        aria-selected={selected}
                        className="st-palette-option"
                        onPointerMove={() => setActive(flat.indexOf(command))}
                        onClick={() => choose(command)}
                      >
                        <command.Icon size={16} aria-hidden="true" />
                        <span>{command.label}</span>
                        {command.hint ? <span className="st-palette-hint">{command.hint}</span> : null}
                      </li>
                    );
                  })}
                </ul>
              </li>
            ))}
          </ul>
        ) : (
          <div className="st-palette-empty">Nothing matches &ldquo;{query}&rdquo;. Try &ldquo;plans&rdquo;, &ldquo;agent&rdquo; or &ldquo;approvals&rdquo;.</div>
        )}
        <div className="st-palette-foot" aria-hidden="true">
          <span>
            <kbd className="st-kbd">↑</kbd> <kbd className="st-kbd">↓</kbd> move
          </span>
          <span>
            <kbd className="st-kbd">Enter</kbd> open
          </span>
          <span>
            <kbd className="st-kbd">Esc</kbd> close
          </span>
        </div>
      </dialog>
    </>
  );
}

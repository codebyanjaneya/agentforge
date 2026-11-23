"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { signIn, useSession } from "next-auth/react";
import { Activity, Ellipsis, Gauge, LayoutTemplate, ListChecks, Plus, Settings, ShieldCheck, UserPlus, Users, Wrench, type LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { AuthButton } from "../auth-button";
import { ThemeToggle } from "../theme-toggle";
import { DemoBanner } from "./_home";
import { Skeleton } from "./_ui";
import { useInboxCount, WorkspaceSwitcher } from "./_team";
import "./studio.css";

type NavItem = { href: string; label: string; Icon: LucideIcon; exact?: boolean };

/** The product has three kinds of place: where work happens, who does it and with what, and who may. The sidebar says so instead of listing eight peers. */
const GROUPS: { label: string; items: NavItem[] }[] = [
  {
    label: "Work",
    items: [
      { href: "/studio", label: "Overview", Icon: Gauge, exact: true },
      { href: "/studio/plans", label: "Plans", Icon: ListChecks },
      { href: "/studio/runs", label: "Runs", Icon: Activity },
      { href: "/studio/approvals", label: "Approvals", Icon: ShieldCheck },
    ],
  },
  {
    label: "Workforce",
    items: [
      { href: "/studio/agents", label: "Agents", Icon: Users },
      { href: "/studio/tools", label: "Tools", Icon: Wrench },
      { href: "/studio/templates", label: "Templates", Icon: LayoutTemplate },
    ],
  },
  {
    label: "Workspace",
    items: [
      { href: "/studio/team", label: "Team", Icon: UserPlus },
      { href: "/studio/settings", label: "Settings", Icon: Settings },
    ],
  },
];
const ALL = GROUPS.flatMap((g) => g.items);
const PRIMARY_MOBILE = ALL.slice(0, 4); // the four places used all day; everything else (and the account) lives under "More"
const MORE_MOBILE = ALL.slice(4);

const isActive = (item: NavItem, pathname: string) => (item.exact ? pathname === item.href : pathname === item.href || pathname.startsWith(`${item.href}/`));

function Shell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const { counts, announcement } = useInboxCount();
  return (
    <>
      <div className="sr-only" role="status" aria-live="polite" aria-atomic="true" data-testid="inbox-announcer">
        {announcement}
      </div>
      <Frame pathname={pathname} pending={counts?.pending ?? 0} status="authenticated">
        {children}
      </Frame>
    </>
  );
}

export default function StudioLayout({ children }: { children: ReactNode }) {
  const { status } = useSession();
  const pathname = usePathname();
  if (status === "authenticated") return <Shell>{children}</Shell>;
  return (
    <Frame pathname={pathname} pending={0} status={status}>
      {children}
    </Frame>
  );
}

function NavLink({ item, pathname, pending }: { item: NavItem; pathname: string; pending: number }) {
  const { href, label, Icon } = item;
  return (
    <Link href={href} aria-current={isActive(item, pathname) ? "page" : undefined}>
      <Icon size={16} aria-hidden="true" />
      <span>{label}</span>
      {href === "/studio/approvals" && pending > 0 ? (
        <span className="st-count" data-testid="inbox-badge">
          {pending}
          <span className="sr-only"> {pending === 1 ? "approval is" : "approvals are"} waiting</span>
        </span>
      ) : null}
    </Link>
  );
}

function Frame({ children, pathname, pending, status }: { children: ReactNode; pathname: string; pending: number; status: string }) {
  const here = ALL.find((item) => isActive(item, pathname));
  return (
    <div className="st">
      <a href="#main" className="st-skip">
        Skip to content
      </a>
      <div className="st-shell">
        <aside className="st-side" aria-label="Studio">
          <Link href="/studio" className="st-brand">
            <span className="st-brand-mark" aria-hidden="true">
              ⌁
            </span>
            AgentForge
          </Link>
          {status === "authenticated" ? <WorkspaceSwitcher /> : null}
          <nav className="st-nav" aria-label="Primary">
            {GROUPS.map((group) => (
              <div key={group.label} className="st-nav-group" role="group" aria-label={group.label}>
                <div className="st-nav-label" aria-hidden="true">
                  {group.label}
                </div>
                {group.items.map((item) => (
                  <NavLink key={item.href} item={item} pathname={pathname} pending={pending} />
                ))}
              </div>
            ))}
          </nav>
          <div className="st-side-foot">
            <AuthButton />
            <div className="st-side-tools">
              <ThemeToggle />
              <Link href="/" className="st-muted text-xs no-underline hover:underline">
                Classic dashboard
              </Link>
            </div>
          </div>
        </aside>
        <div className="min-w-0">
          <header className="st-topbar">
            <Link href="/studio" className="st-brand st-brand-compact">
              <span className="st-brand-mark" aria-hidden="true">
                ⌁
              </span>
              AgentForge
            </Link>
            <div className="st-crumb" aria-hidden="true">
              {here?.label ?? "Studio"}
            </div>
            <div className="st-topbar-end">
              {status === "authenticated" ? <DemoBanner /> : null}
              {status === "authenticated" ? (
                <Link href="/studio#objective" className="st-btn st-btn-primary st-btn-sm st-topbar-new" aria-label="New objective">
                  <Plus size={15} aria-hidden="true" />
                  <span>New objective</span>
                </Link>
              ) : null}
              <span className="st-topbar-theme">
                <ThemeToggle />
              </span>
            </div>
          </header>
          <main className="st-main" id="main" tabIndex={-1}>
            {status === "loading" ? (
              <Skeleton lines={4} height="4rem" />
            ) : status === "authenticated" ? (
              children
            ) : (
              <div className="st-panel mx-auto mt-16 max-w-md p-8 text-center">
                <div className="st-h2">Sign in to open the Studio</div>
                <p className="st-lede mx-auto">Your agents, plans and runs are private to your account.</p>
                <button className="st-btn st-btn-primary mt-5" onClick={() => void signIn("google")}>
                  Sign in with Google
                </button>
              </div>
            )}
          </main>
        </div>
      </div>
      <nav className="st-tabbar" aria-label="Studio, compact">
        {PRIMARY_MOBILE.map((item) => (
          <NavLink key={item.href} item={item} pathname={pathname} pending={pending} />
        ))}
        <details
          key={pathname}
          className="st-more"
          onKeyDown={(event) => {
            if (event.key === "Escape" && event.currentTarget.open) {
              event.currentTarget.open = false; // a disclosure closes on Escape and hands focus back to its button
              event.currentTarget.querySelector("summary")?.focus();
            }
          }}
        >
          <summary aria-label="More">
            <Ellipsis size={16} aria-hidden="true" />
            <span>More</span>
          </summary>
          <div className="st-more-sheet">
            {MORE_MOBILE.map((item) => (
              <NavLink key={item.href} item={item} pathname={pathname} pending={pending} />
            ))}
            <div className="st-more-account">
              <AuthButton />
              <Link href="/" className="st-muted text-xs no-underline hover:underline">
                Classic dashboard
              </Link>
            </div>
          </div>
        </details>
      </nav>
    </div>
  );
}

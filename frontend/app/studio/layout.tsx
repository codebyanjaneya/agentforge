"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { signIn, useSession } from "next-auth/react";
import { Activity, Gauge, LayoutTemplate, ListChecks, ShieldCheck, UserPlus, Users, Wrench, type LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { AuthButton } from "../auth-button";
import { ThemeToggle } from "../theme-toggle";
import { DemoBanner } from "./_home";
import { Skeleton } from "./_ui";
import { useInboxCount, WorkspaceSwitcher } from "./_team";
import "./studio.css";

const NAV: { href: string; label: string; Icon: LucideIcon; exact?: boolean }[] = [
  { href: "/studio", label: "Overview", Icon: Gauge, exact: true },
  { href: "/studio/plans", label: "Plans", Icon: ListChecks },
  { href: "/studio/runs", label: "Runs", Icon: Activity },
  { href: "/studio/approvals", label: "Approvals", Icon: ShieldCheck },
  { href: "/studio/agents", label: "Agents", Icon: Users },
  { href: "/studio/tools", label: "Tools", Icon: Wrench },
  { href: "/studio/templates", label: "Templates", Icon: LayoutTemplate },
  { href: "/studio/team", label: "Team", Icon: UserPlus },
];

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

function Frame({ children, pathname, pending, status }: { children: ReactNode; pathname: string; pending: number; status: string }) {
  return (
    <div className="st">
      <div className="st-shell">
        <aside className="st-side" aria-label="Studio">
          <Link href="/studio" className="st-brand">
            <span className="st-brand-mark" aria-hidden="true">
              ⌁
            </span>
            AgentForge Studio
          </Link>
          <nav className="st-nav" aria-label="Primary">
            {NAV.map(({ href, label, Icon, exact }) => {
              const active = exact ? pathname === href : pathname === href || pathname.startsWith(`${href}/`);
              return (
                <Link key={href} href={href} aria-current={active ? "page" : undefined}>
                  <Icon size={17} aria-hidden="true" />
                  {label}
                  {href === "/studio/approvals" && pending > 0 ? (
                    <span className="st-count" data-testid="inbox-badge">
                      {pending}
                      <span className="sr-only"> {pending === 1 ? "approval is" : "approvals are"} waiting</span>
                    </span>
                  ) : null}
                </Link>
              );
            })}
          </nav>
          <div className="st-side-foot mt-auto grid gap-3">
            {status === "authenticated" ? <WorkspaceSwitcher /> : null}
            <Link href="/" className="st-muted text-xs no-underline hover:underline">
              ← Classic dashboard
            </Link>
            <div className="flex items-center gap-2">
              <ThemeToggle />
            </div>
            <AuthButton />
          </div>
        </aside>
        <div className="min-w-0">
          <div className="st-topbar">
            <Link href="/studio" className="st-brand">
              <span className="st-brand-mark" aria-hidden="true">
                ⌁
              </span>
              Studio
            </Link>
            <div className="flex items-center gap-2">
              <ThemeToggle />
            </div>
          </div>
          <main className="st-main" id="main">
            {status === "loading" ? (
              <Skeleton lines={4} height="4rem" />
            ) : status === "authenticated" ? (
              <>
                <DemoBanner />
                {children}
              </>
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
    </div>
  );
}

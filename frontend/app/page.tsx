import type { Metadata } from "next";
import Link from "next/link";
import { CONTROL_LINES } from "@/lib/studio/home";
import { LandingCta, LandingNavEnd } from "./_landing/landing-client";
import { TaskGraph, type GraphTask } from "./studio/_graph";
import "./studio/studio.css";
import "./studio/foundation.css";

export const metadata: Metadata = {
  title: "AgentForge: AI agents that do real work, with you in control",
  description:
    "AgentForge plans an objective with specialist AI agents, runs it with real tools, and stops to ask before anything sensitive. You approve the plan, then each exact action, and read everything that happened.",
  openGraph: {
    title: "AgentForge: AI agents that do real work, with you in control",
    description: "Specialist agents plan and run the work. You approve the plan and every sensitive action.",
    type: "website",
  },
};

/** An ILLUSTRATION, labelled as one on the page: the shape of a real run (the same graph component the Studio uses), not live data and not a customer. */
const EXAMPLE_TASKS: GraphTask[] = [
  { id: "economics", title: "Check the unit economics", depends_on: [], level: 0, state: "succeeded" },
  { id: "dates", title: "Set the launch date", depends_on: [], level: 0, state: "succeeded" },
  { id: "announce", title: "Tell the first customer", depends_on: ["economics", "dates"], level: 1, state: "running" },
];

const STEPS = [
  {
    title: "Assemble your workforce",
    text: "An agent is a specialist: written instructions, the tools it may use, and a model. Start with three ready-made specialists or create your own. Every change is saved as a new version.",
  },
  {
    title: "Delegate an objective",
    text: "Describe the outcome you want. AgentForge drafts a plan: the tasks, the agent for each one, and what depends on what. Nothing runs yet.",
  },
  {
    title: "Approve the plan, then every sensitive action",
    text: "You approve the plan and start the run as separate steps. When an agent needs to act outside AgentForge it stops, shows the exact tool and values, and waits for you.",
  },
  {
    title: "Follow the run and read the history",
    text: "Watch tasks finish, see every tool call and its result, and keep a record of what was approved and what happened.",
  },
];

const WORKS_TODAY = [
  "Plan, approve and run objectives with specialist agents and a visible dependency graph.",
  "Built-in tools: exact calculation, unit economics, date arithmetic, text analysis, table summaries and public-holiday lookups.",
  "Approval of each exact sensitive action by a workspace owner or admin, kept safe across restarts.",
  "Shared workspaces with owner, admin and member roles, and invitations by link.",
  "Run history, tool-call history, and token and cost figures taken from the model provider's own usage reports.",
];

const NOT_YET = [
  "Email sending is limited to the provider's test recipients by default, and delivery to real recipients has not yet been verified. For outside services there is no exactly-once guarantee: an unknown outcome is shown to a person to resolve, never retried silently.",
  "AgentForge does not send invitation emails yet: you copy the invitation link.",
  "Demo mode uses a scripted model, and a successful run against a live model has not yet been verified. Plans are written by whichever model the operator configured; a model can be wrong, which is why you approve before anything runs.",
  "No independent security audit yet.",
];

export default function Landing() {
  return (
    <div className="st lp">
      <a href="#main" className="st-skip">
        Skip to content
      </a>
      <header className="lp-nav">
        <div className="lp-wrap">
          <Link href="/" className="st-brand" aria-label="AgentForge, home">
            <span className="st-brand-mark" aria-hidden="true">
              ⌁
            </span>
            AgentForge
          </Link>
          <nav className="lp-nav-links" aria-label="On this page">
            <a href="#how">How it works</a>
            <a href="#control">Control</a>
            <a href="#today">What is real today</a>
          </nav>
          <LandingNavEnd />
        </div>
      </header>

      <main id="main" tabIndex={-1}>
        <section className="lp-hero" aria-labelledby="hero-title">
          <div className="lp-wrap lp-hero-grid">
            <div>
              <div className="st-eyebrow st-rise">Autonomous workforce, under your control</div>
              <h1 className="lp-h1 st-rise" id="hero-title" style={{ animationDelay: "40ms" }}>
                Real work, handed to AI agents. Every decision still yours.
              </h1>
              <p className="lp-sub st-rise" style={{ animationDelay: "90ms" }}>
                AgentForge plans an objective with specialist agents, runs it with real tools, and stops to ask before anything sensitive. You approve the plan, then each
                exact action, and you can read everything that happened.
              </p>
              <div className="lp-cta st-rise" style={{ animationDelay: "140ms" }}>
                <LandingCta />
                <a href="#how" className="st-btn st-btn-lg">
                  See how it works
                </a>
              </div>
              <p className="lp-note">Early access. Sign in with your Google account.</p>
            </div>

            <figure className="st-panel lp-visual st-rise" style={{ animationDelay: "120ms", margin: 0 }} aria-labelledby="visual-caption">
              <div className="lp-visual-head">
                <span className="st-eyebrow">Example run: launch readiness</span>
                <span className="st-hint" id="visual-caption">
                  Illustration, not live data
                </span>
              </div>
              <div className="lp-visual-body">
                <TaskGraph tasks={EXAMPLE_TASKS} caption="Example plan: two checks run together, then the announcement" />
                <div className="lp-approval">
                  <div className="st-eyebrow" style={{ color: "var(--st-ember-text)" }}>
                    Waiting for your approval
                  </div>
                  <dl className="lp-kv">
                    <dt>Tool</dt>
                    <dd className="st-mono">email.send@1</dd>
                    <dt>To</dt>
                    <dd>first.customer@example.com</dd>
                    <dt>Subject</dt>
                    <dd>Your plan launches on the agreed date</dd>
                  </dl>
                  <p className="st-muted m-0 text-sm">The agent paused at this exact call. An owner or admin approves or rejects it; nothing else is sent.</p>
                </div>
              </div>
            </figure>
          </div>
        </section>

        <section className="lp-section" id="how" aria-labelledby="how-title">
          <div className="lp-wrap lp-how">
            <div className="lp-section-head">
              <div className="st-eyebrow">How it works</div>
              <h2 className="lp-h2" id="how-title">
                From objective to audited result, in four steps
              </h2>
              <p className="lp-lede">Each step is something you do, not something assumed for you. You can stop after any of them.</p>
            </div>
            <ol className="lp-steps">
              {STEPS.map((step, i) => (
                <li key={step.title}>
                  <span className="lp-node" aria-hidden="true">
                    {i + 1}
                  </span>
                  <div>
                    <h3 className="lp-step-title">{step.title}</h3>
                    <p className="lp-step-text">{step.text}</p>
                  </div>
                </li>
              ))}
            </ol>
          </div>
        </section>

        <section className="lp-section" id="control" aria-labelledby="control-title">
          <div className="lp-wrap">
            <div className="lp-section-head">
              <div className="st-eyebrow">Control</div>
              <h2 className="lp-h2" id="control-title">
                Control is built in, not promised
              </h2>
            </div>
            <ul className="lp-control">
              {CONTROL_LINES.map((line) => (
                <li key={line.name}>
                  <h3>{line.name}</h3>
                  <p>{line.text}</p>
                </li>
              ))}
            </ul>
          </div>
        </section>

        <section className="lp-section" id="today" aria-labelledby="today-title">
          <div className="lp-wrap">
            <div className="lp-section-head">
              <div className="st-eyebrow">What is real today</div>
              <h2 className="lp-h2" id="today-title">
                Exactly what works, and what does not yet
              </h2>
              <p className="lp-lede">We would rather you know the limits before you rely on it.</p>
            </div>
            <div className="lp-today">
              <div>
                <h3>Works today</h3>
                <ul className="lp-list">
                  {WORKS_TODAY.map((t) => (
                    <li key={t}>{t}</li>
                  ))}
                </ul>
              </div>
              <div>
                <h3>Honest limits</h3>
                <ul className="lp-list" data-tone="todo">
                  {NOT_YET.map((t) => (
                    <li key={t}>{t}</li>
                  ))}
                </ul>
              </div>
            </div>
          </div>
        </section>

        <div className="lp-wrap">
          <section className="st-panel lp-final" aria-labelledby="final-title">
            <div>
              <h2 className="lp-h2" id="final-title" style={{ marginTop: 0 }}>
                Start with one objective.
              </h2>
              <p className="lp-lede" style={{ marginTop: "0.5rem" }}>
                You will see the plan before anything runs.
              </p>
            </div>
            <LandingCta />
          </section>
        </div>
      </main>

      <footer className="lp-footer">
        <div className="lp-wrap">
          <span>
            <span aria-hidden="true">⌁ </span>AgentForge · Early access
          </span>
          <span>
            <Link href="/signin">Sign in</Link> · <Link href="/studio">Studio</Link>
          </span>
        </div>
      </footer>
    </div>
  );
}

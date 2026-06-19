"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

interface Kpi {
  label: string;
  value: string;
  hint: string;
  accent: string;
}

const KPIS: Kpi[] = [
  {
    label: "Total Revenue",
    value: "$24,500",
    hint: "▲ trending up",
    accent: "text-green-400",
  },
  {
    label: "Active Leads",
    value: "127",
    hint: "pipeline",
    accent: "text-blue-400",
  },
  {
    label: "Team Size",
    value: "8",
    hint: "headcount",
    accent: "text-purple-400",
  },
  {
    label: "Burn Rate",
    value: "$8,200/mo",
    hint: "▼ watch closely",
    accent: "text-red-400",
  },
  {
    label: "Runway",
    value: "4.2 months",
    hint: "at current burn",
    accent: "text-amber-400",
  },
  {
    label: "MRR Growth",
    value: "+18%",
    hint: "▲ month over month",
    accent: "text-green-400",
  },
];

const REVENUE = [
  { month: "Jan", value: 8000 },
  { month: "Feb", value: 11000 },
  { month: "Mar", value: 14000 },
  { month: "Apr", value: 18000 },
  { month: "May", value: 21000 },
  { month: "Jun", value: 24500 },
];

const ACTIVITY_TEMPLATES = [
  "Sales Agent found {n} new leads",
  "Marketing Agent published a blog post",
  "Marketing Agent launched a {channel} campaign",
  "Research Agent analyzed {n} competitors",
  "Support Agent resolved {n} tickets",
  "Finance Agent flagged a burn-rate alert",
  "Hiring Agent screened {n} resumes",
  "CEO Agent updated the weekly strategy",
  "Sales Agent closed a ${n}K deal",
  "Research Agent identified a new market trend",
];

const CHANNELS = ["LinkedIn", "Twitter", "email", "SEO", "paid search"];

interface Activity {
  id: number;
  text: string;
  time: string;
}

function randomInt(min: number, max: number) {
  return Math.floor(Math.random() * (max - min + 1)) + min;
}

function buildActivity(id: number): Activity {
  const template =
    ACTIVITY_TEMPLATES[randomInt(0, ACTIVITY_TEMPLATES.length - 1)];
  const text = template
    .replace("{n}", String(randomInt(2, 25)))
    .replace("{channel}", CHANNELS[randomInt(0, CHANNELS.length - 1)]);
  return {
    id,
    text,
    time: new Date().toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }),
  };
}

export default function DigitalTwin() {
  const maxRevenue = Math.max(...REVENUE.map((r) => r.value));
  const idRef = useRef(0);
  const [activities, setActivities] = useState<Activity[]>([]);

  useEffect(() => {
    const seed: Activity[] = Array.from({ length: 5 }, () =>
      buildActivity(idRef.current++)
    );
    setActivities(seed);

    const interval = setInterval(() => {
      setActivities((prev) =>
        [buildActivity(idRef.current++), ...prev].slice(0, 12)
      );
    }, 5000);

    return () => clearInterval(interval);
  }, []);

  return (
    <main className="min-h-screen w-full bg-gray-950 px-4 py-10 sm:px-8">
      <div className="mx-auto max-w-7xl">
        <header className="mb-10 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <h1 className="text-4xl font-bold tracking-tight text-white">
              Digital Twin
            </h1>
            <p className="mt-1 text-gray-400">Live Startup Simulation</p>
          </div>
          <Link
            href="/"
            className="inline-flex w-fit items-center gap-2 rounded-lg border border-gray-700 bg-gray-900/60 px-4 py-2 text-sm font-medium text-gray-200 transition-colors hover:border-gray-500 hover:bg-gray-800"
          >
            ← Back to Agents
          </Link>
        </header>

        <section className="mb-8 grid grid-cols-2 gap-4 md:grid-cols-3 lg:grid-cols-6">
          {KPIS.map((kpi) => (
            <div
              key={kpi.label}
              className="rounded-2xl border border-gray-800 bg-gray-900/60 p-4 shadow-lg backdrop-blur"
            >
              <p className="text-xs font-medium uppercase tracking-wide text-gray-500">
                {kpi.label}
              </p>
              <p className={`mt-2 text-2xl font-bold ${kpi.accent}`}>
                {kpi.value}
              </p>
              <p className="mt-1 text-xs text-gray-500">{kpi.hint}</p>
            </div>
          ))}
        </section>

        <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
          <section className="rounded-2xl border border-gray-800 bg-gray-900/60 p-6 shadow-lg backdrop-blur lg:col-span-2">
            <div className="mb-6 flex items-center justify-between">
              <h2 className="text-lg font-semibold text-white">
                Revenue — Last 6 Months
              </h2>
              <span className="text-sm font-medium text-green-400">
                ${maxRevenue.toLocaleString()}
              </span>
            </div>
            <div className="flex h-56 items-end gap-3 sm:gap-6">
              {REVENUE.map((point) => {
                const height = Math.round((point.value / maxRevenue) * 100);
                return (
                  <div
                    key={point.month}
                    className="flex h-full flex-1 flex-col items-center justify-end gap-2"
                  >
                    <span className="text-xs font-medium text-gray-400">
                      ${(point.value / 1000).toFixed(1)}k
                    </span>
                    <div
                      className="w-full rounded-t-md bg-gradient-to-t from-purple-600 to-blue-400 transition-all"
                      style={{ height: `${height}%`, minHeight: "8px" }}
                    />
                    <span className="text-xs text-gray-500">{point.month}</span>
                  </div>
                );
              })}
            </div>
          </section>

          <section className="rounded-2xl border border-gray-800 bg-gray-900/60 p-6 shadow-lg backdrop-blur">
            <div className="mb-4 flex items-center justify-between">
              <h2 className="text-lg font-semibold text-white">
                Agent Activity
              </h2>
              <span className="flex items-center gap-1.5 text-xs text-green-400">
                <span className="h-2 w-2 animate-pulse rounded-full bg-green-400" />
                Live
              </span>
            </div>
            <ul className="flex max-h-72 flex-col gap-3 overflow-y-auto pr-1">
              {activities.map((activity) => (
                <li
                  key={activity.id}
                  className="flex items-start gap-3 rounded-lg border border-gray-800 bg-gray-950/60 p-3"
                >
                  <span className="mt-1 h-2 w-2 flex-shrink-0 rounded-full bg-blue-400" />
                  <div>
                    <p className="text-sm text-gray-200">{activity.text}</p>
                    <p className="text-xs text-gray-500">{activity.time}</p>
                  </div>
                </li>
              ))}
            </ul>
          </section>
        </div>

        <section className="mt-6 rounded-2xl border border-amber-500/30 bg-gray-900/60 p-6 shadow-lg backdrop-blur">
          <h2 className="mb-4 flex items-center gap-2 text-lg font-semibold text-white">
            <span>🧠</span> CEO Predictions
          </h2>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
            <div className="rounded-lg border border-gray-800 bg-gray-950/60 p-4">
              <p className="text-xs uppercase tracking-wide text-gray-500">
                Next Month Revenue Forecast
              </p>
              <p className="mt-2 text-2xl font-bold text-green-400">$29,400</p>
            </div>
            <div className="rounded-lg border border-gray-800 bg-gray-950/60 p-4">
              <p className="text-xs uppercase tracking-wide text-gray-500">
                Leads Needed to Hit Target
              </p>
              <p className="mt-2 text-2xl font-bold text-blue-400">45</p>
            </div>
            <div className="rounded-lg border border-gray-800 bg-gray-950/60 p-4">
              <p className="text-xs uppercase tracking-wide text-gray-500">
                Recommended Action
              </p>
              <p className="mt-2 text-lg font-semibold text-amber-400">
                Scale marketing spend
              </p>
            </div>
          </div>
        </section>
      </div>
    </main>
  );
}

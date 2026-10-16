import site from "./data/site.json";
import type { SiteData } from "./types";
import { AgentConsole } from "./components/AgentConsole";
import { Footer } from "./components/Footer";
import { Hero } from "./components/Hero";
import { HowItWorks } from "./components/HowItWorks";
import { Nav } from "./components/Nav";
import { Problem } from "./components/Problem";
import { Results } from "./components/Results";

const data = site as unknown as SiteData;

/** The run shown in the hero: webhooks say failed, the gateway says paid. */
const FEATURED_SCENARIO = "verify_mismatch_stale_success";

export default function App() {
  const featured = data.scenarios.find((r) => r.scenario.scenario === FEATURED_SCENARIO) ?? data.scenarios[0];
  return (
    <>
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-50 focus:rounded-full focus:bg-ink focus:px-4 focus:py-2 focus:text-bg"
      >
        Skip to content
      </a>
      <Nav />
      <main id="main">
        <Hero featured={featured} />
        <Problem />
        <AgentConsole runs={data.scenarios} />
        <Results metrics={data.metrics} baseline={data.baseline} />
        <HowItWorks />
      </main>
      <Footer />
    </>
  );
}

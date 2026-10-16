import { GithubLogo } from "@phosphor-icons/react";
import { SOURCE_URL } from "./ui";

const LINKS = [
  { href: "#agent", label: "Agent" },
  { href: "#results", label: "Results" },
  { href: "#how", label: "How it works" },
];

export function Nav() {
  return (
    <header className="fixed inset-x-0 top-4 z-30 flex justify-center px-4">
      <nav
        aria-label="Main"
        className="flex h-14 w-full max-w-[680px] items-center justify-between gap-4 rounded-full bg-surface/80 pl-5 pr-2 shadow-[inset_0_0_0_1px_var(--line),var(--shadow)] backdrop-blur-xl"
      >
        <a href="#top" translate="no" className="text-[15px] font-semibold tracking-[-0.02em]">
          Revora
        </a>
        <ul className="hidden items-center gap-1 text-sm text-muted sm:flex">
          {LINKS.map((link) => (
            <li key={link.href}>
              <a
                href={link.href}
                className="rounded-full px-3 py-2 transition-colors duration-300 hover:bg-core hover:text-ink"
              >
                {link.label}
              </a>
            </li>
          ))}
        </ul>
        <a
          href={SOURCE_URL}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-2 rounded-full bg-ink px-4 py-2 text-sm font-medium text-bg transition-colors duration-300 hover:bg-accent hover:text-accent-ink"
        >
          <GithubLogo size={16} weight="light" aria-hidden />
          Source
        </a>
      </nav>
    </header>
  );
}

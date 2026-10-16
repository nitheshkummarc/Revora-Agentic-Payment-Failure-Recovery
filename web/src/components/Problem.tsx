const FACTS = [
  {
    fact: "Webhooks arrive late, twice, out of order, or not at all.",
    source: "Razorpay webhook docs: delivery is at least once and order is not guaranteed.",
    href: "https://razorpay.com/docs/webhooks/best-practices/",
  },
  {
    fact: "A payment marked failed can turn authorized minutes later.",
    source: "Razorpay documents late authorization as a normal case to handle.",
    href: "https://razorpay.com/docs/payments/payments/late-authorisation/handle",
  },
  {
    fact: "Retrying a mandate is regulated: 24-hour notice, a ceiling, extra authentication above ₹15,000.",
    source: "RBI Digital Payments E-Mandate Framework, 2026 (RBI/DPSS/2026-27/396).",
    href: "https://www.rbi.org.in",
  },
];

export function Problem() {
  return (
    <section className="border-y border-line bg-surface">
      <div className="mx-auto grid max-w-[1240px] gap-12 px-5 py-24 md:px-8 lg:grid-cols-[1fr_1fr] lg:py-32">
        <h2 className="max-w-[18ch] text-[clamp(2rem,3.6vw,3rem)] font-medium leading-[1.08] tracking-[-0.035em]">
          A retry bot reading stale evidence will eventually charge someone twice.
        </h2>
        <ul className="divide-y divide-line">
          {FACTS.map((item) => (
            <li key={item.fact} className="py-6 first:pt-0 last:pb-0">
              <p className="text-lg leading-snug">{item.fact}</p>
              <a
                href={item.href}
                target="_blank"
                rel="noreferrer"
                className="mt-2 inline-block text-sm text-muted underline decoration-line-strong underline-offset-4 transition-colors hover:text-ink"
              >
                {item.source}
              </a>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}

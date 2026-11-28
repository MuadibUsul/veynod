import type { Metadata } from "next";

import { docsPath, site, type DocsLang } from "./site";

export const SITE_URL = "https://veynod.com";
export const DOCS_HOME_URL = `${SITE_URL}/docs/`;
// Update when the generated docs content changes; keep it a build-time
// constant so every page reports a stable dateModified.
export const DOCS_LAST_MODIFIED = "2026-11-28";

export const OG_IMAGE = {
  url: `${SITE_URL}/og.png`,
  width: 1200,
  height: 630,
  alt: "Veynod — Shared Blackboard & MCP Control Plane for AI Agent Teams",
};

export function docsUrl(slug: string, lang: DocsLang = "en"): string {
  return `${SITE_URL}${docsPath(slug, lang)}`;
}

/** Title and description a search engine shows for a docs page. */
export function docsSeo(slug: string, lang: DocsLang): { title: string; description: string } {
  const seo = site.pages[slug]?.seo;
  return {
    title: seo?.title[lang] || seo?.title.en || "Veynod Docs",
    description: seo?.description[lang] || seo?.description.en || "",
  };
}

export function docsMetadata({ slug, lang }: { slug: string; lang: DocsLang }): Metadata {
  const { title, description } = docsSeo(slug, lang);
  const url = docsUrl(slug, lang);
  const article = slug !== "introduction";
  const openGraph = {
    type: article ? "article" : "website",
    siteName: "Veynod",
    title,
    description,
    url,
    images: [OG_IMAGE],
    locale: lang === "zh" ? "zh_CN" : "en_US",
    alternateLocale: lang === "zh" ? ["en_US"] : ["zh_CN"],
    ...(article ? { modifiedTime: DOCS_LAST_MODIFIED } : {}),
  } as Metadata["openGraph"];
  return {
    title: { absolute: title },
    description,
    alternates: {
      canonical: url,
      languages: {
        en: docsUrl(slug, "en"),
        zh: docsUrl(slug, "zh"),
        "zh-Hans": docsUrl(slug, "zh"),
        "x-default": docsUrl(slug, "en"),
      },
    },
    icons: {
      icon: [
        { url: "/favicon.ico", sizes: "48x48" },
        { url: "/favicon.svg", type: "image/svg+xml" },
      ],
      apple: "/apple-touch-icon.png",
    },
    openGraph,
    twitter: {
      card: "summary_large_image",
      title,
      description,
      images: [OG_IMAGE.url],
    },
    robots: { index: true, follow: true },
  };
}

export function docsPageJsonLd({ slug, lang }: { slug: string; lang: DocsLang }) {
  const url = docsUrl(slug, lang);
  const { title, description } = docsSeo(slug, lang);
  const home = docsUrl("introduction", lang);
  const graph: Record<string, unknown>[] = [
    {
      "@type": "TechArticle",
      "@id": `${url}#article`,
      headline: title,
      description,
      url,
      inLanguage: lang === "zh" ? "zh-Hans" : "en",
      mainEntityOfPage: { "@type": "WebPage", "@id": url },
      dateModified: DOCS_LAST_MODIFIED,
      isPartOf: {
        "@type": "WebSite",
        "@id": `${SITE_URL}/#website`,
        name: "Veynod",
        url: `${SITE_URL}/`,
      },
      // The same entity the home page declares (https://veynod.com/#organization).
      publisher: {
        "@type": "Organization",
        "@id": `${SITE_URL}/#organization`,
        name: "Veynod",
        url: `${SITE_URL}/`,
        logo: {
          "@type": "ImageObject",
          url: `${SITE_URL}/apple-touch-icon.png`,
        },
      },
    },
  ];
  const breadcrumb = [
    { "@type": "ListItem", position: 1, name: "Veynod", item: lang === "zh" ? `${SITE_URL}/zh/` : `${SITE_URL}/` },
    { "@type": "ListItem", position: 2, name: lang === "zh" ? "文档" : "Docs", item: home },
  ];
  if (slug !== "introduction") {
    const name = site.nav.flatMap((group) => group.pages).find((page) => page.slug === slug)?.title[lang];
    breadcrumb.push({ "@type": "ListItem", position: 3, name: name || title, item: url });
  }
  graph.push({
    "@type": "BreadcrumbList",
    "@id": `${url}#breadcrumb`,
    itemListElement: breadcrumb,
  });
  if (slug === "faq") {
    const questions = faqQuestions(lang);
    if (questions.length) {
      graph.push({
        "@type": "FAQPage",
        "@id": `${url}#faq`,
        mainEntity: questions.map(({ question, answer }) => ({
          "@type": "Question",
          name: question,
          acceptedAnswer: { "@type": "Answer", text: answer },
        })),
      });
    }
  }
  return { "@context": "https://schema.org", "@graph": graph };
}

function faqQuestions(lang: DocsLang): Array<{ question: string; answer: string }> {
  const blocks = site.pages.faq?.blocks ?? [];
  const questions: Array<{ question: string; answer: string }> = [];
  for (let index = 0; index < blocks.length - 1; index += 1) {
    const question = blocks[index];
    const answer = blocks[index + 1];
    if (question.type === "h" && answer.type === "p") {
      questions.push({ question: question.text[lang], answer: answer.text[lang] });
    }
  }
  return questions;
}

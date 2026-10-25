import type { Metadata } from "next";

import { site } from "./site";

export const SITE_URL = "https://veynod.com";
export const DOCS_HOME_URL = `${SITE_URL}/docs/`;
// Update when the generated docs content changes; keep it a build-time
// constant so every page reports a stable dateModified.
export const DOCS_LAST_MODIFIED = "2026-10-25";

export const OG_IMAGE = {
  url: `${SITE_URL}/og.png`,
  width: 1200,
  height: 630,
  alt: "Veynod — A shared blackboard for agent teams",
};

export function docsUrl(slug: string): string {
  return slug === "introduction" ? DOCS_HOME_URL : `${SITE_URL}/docs/${slug}/`;
}

export function docsMetadata({
  title,
  description,
  slug,
  article,
}: {
  title: string;
  description: string;
  slug: string;
  article: boolean;
}): Metadata {
  const url = docsUrl(slug);
  const openGraph = {
    type: article ? "article" : "website",
    siteName: "Veynod",
    title,
    description,
    url,
    images: [OG_IMAGE],
    locale: "en_US",
    ...(article ? { modifiedTime: DOCS_LAST_MODIFIED } : {}),
  } as Metadata["openGraph"];
  return {
    title,
    description,
    alternates: { canonical: url },
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

export function docsPageJsonLd({
  slug,
  title,
  description,
}: {
  slug: string;
  title: string;
  description: string;
}) {
  const url = docsUrl(slug);
  const graph: Record<string, unknown>[] = [
    {
      "@type": "TechArticle",
      "@id": `${url}#article`,
      headline: title,
      description,
      url,
      mainEntityOfPage: { "@type": "WebPage", "@id": url },
      dateModified: DOCS_LAST_MODIFIED,
      isPartOf: {
        "@type": "WebSite",
        name: "Veynod Docs",
        url: DOCS_HOME_URL,
      },
      publisher: {
        "@type": "Organization",
        name: "Veynod",
        url: SITE_URL,
      },
    },
  ];
  const breadcrumb = [
    { "@type": "ListItem", position: 1, name: "Veynod", item: `${SITE_URL}/` },
    { "@type": "ListItem", position: 2, name: "Docs", item: DOCS_HOME_URL },
  ];
  if (slug !== "introduction") {
    breadcrumb.push({
      "@type": "ListItem",
      position: 3,
      name: title,
      item: url,
    });
  }
  graph.push({
    "@type": "BreadcrumbList",
    "@id": `${url}#breadcrumb`,
    itemListElement: breadcrumb,
  });
  if (slug === "faq") {
    const questions = faqQuestions();
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

function faqQuestions(): Array<{ question: string; answer: string }> {
  const blocks = site.pages.faq?.blocks ?? [];
  const questions: Array<{ question: string; answer: string }> = [];
  for (let index = 0; index < blocks.length - 1; index += 1) {
    const question = blocks[index];
    const answer = blocks[index + 1];
    if (question.type === "h" && answer.type === "p") {
      questions.push({ question: question.text.en, answer: answer.text.en });
    }
  }
  return questions;
}

import type { Metadata } from "next";

import { DocsView } from "../docs-view";
import { docsMetadata, docsPageJsonLd } from "../seo";
import { pageTitle, site } from "../site";

export const dynamicParams = false;

export function generateStaticParams() {
  return Object.keys(site.pages)
    .filter((slug) => slug !== "introduction")
    .map((slug) => ({ slug }));
}

export function generateMetadata({ params }: { params: { slug: string } }): Metadata {
  const title = `${pageTitle(params.slug)} · Veynod Docs`;
  const description = site.pages[params.slug]?.summary.en ?? "";
  return docsMetadata({ title, description, slug: params.slug, article: true });
}

export default function DocsPage({ params }: { params: { slug: string } }) {
  const description = site.pages[params.slug]?.summary.en ?? "";
  const jsonLd = docsPageJsonLd({
    slug: params.slug,
    title: pageTitle(params.slug),
    description,
  });
  return (
    <>
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{
          __html: JSON.stringify(jsonLd).replace(/</g, "\\u003c"),
        }}
      />
      <DocsView slug={params.slug} />
    </>
  );
}

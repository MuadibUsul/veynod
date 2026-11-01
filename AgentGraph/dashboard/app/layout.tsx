import type { Metadata } from "next";
import { I18nProvider } from "./i18n";
import "./design-tokens.css";
import "./globals.css";
import "./v3.css";
import "./cult.css";
import "./brand.css";
import "./focus.css";
import "./vi.css";

export const metadata: Metadata = {
  metadataBase: new URL("https://veynod.com"),
  title: "Veynod Console",
  description:
    "Set the project diagram and watch the agent loop build it: a human-in-the-loop blackboard for multi-agent teams.",
  // The console is a customer surface. Public docs override this.
  robots: { index: false, follow: false },
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="" />
        <link
          rel="stylesheet"
          href="https://fonts.googleapis.com/css2?family=Inter:wght@400..700&family=Geist+Mono:wght@400;500&family=Poppins:wght@700&display=swap"
        />
      </head>
      <body>
        <I18nProvider>{children}</I18nProvider>
      </body>
    </html>
  );
}

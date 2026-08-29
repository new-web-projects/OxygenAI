import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Oxygen AI — minimal slice",
  description: "Deterministic indicator engine + a provider-agnostic AI reasoning layer",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}

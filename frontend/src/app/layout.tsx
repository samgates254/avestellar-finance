import type { Metadata } from "next";
import "./globals.css";
import { SessionProvider } from "@/components/session";
export const metadata: Metadata = { title: "AVESTELLAR FINANCE", description: "Financial control centre" };
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) { return <html lang="en"><body><SessionProvider>{children}</SessionProvider></body></html>; }

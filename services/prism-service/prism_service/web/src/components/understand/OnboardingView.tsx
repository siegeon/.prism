import { Empty } from "@/components/ui";
import Markdown from "@/components/Markdown";

/**
 * Lightweight markdown renderer for the onboarding analyzer output.
 *
 * Borrowed structure from Lum1104/Understand-Anything's LearnPanel
 * (which uses MarkdownIt); we keep it dependency-free. The block parser
 * (H1/H2/H3, paragraphs, unordered lists, inline `code` and **bold**)
 * now lives in the shared <Markdown> renderer — this is a thin wrapper
 * preserving the onboarding empty-state.
 */
export default function OnboardingView({ text }: { text: string }) {
  if (!text || !text.trim()) {
    return <Empty>No onboarding doc yet.</Empty>;
  }
  return <Markdown text={text} />;
}

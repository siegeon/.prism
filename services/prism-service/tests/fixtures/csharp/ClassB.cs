namespace PlatFixture
{
    public class B
    {
        public void Bar()
        {
            // Cross-file invocation — this is the canonical AC-1 / AC-2 case.
            new A().Foo();
        }

        public void CallShadowed()
        {
            // Calls the stoplist-shadowing user method to exercise the
            // stoplist filter (hypothesis 2). A working extractor must
            // emit a 'calls' edge from CallShadowed to ToStringShadow.
            new A().ToStringShadow();
        }
    }
}

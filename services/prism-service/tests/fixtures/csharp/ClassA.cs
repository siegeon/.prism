namespace PlatFixture
{
    public class A
    {
        public void Foo()
        {
            // Body intentionally empty.
        }

        // Method whose name shadows a stoplist token (e.g. ToString).
        // siegeon#45 AC: this user-defined method must still produce a
        // 'calls' edge when invoked from a method body, and must NOT be
        // dropped by the framework stoplist.
        public string ToStringShadow()
        {
            return "shadowed";
        }
    }

    public class Derived : A
    {
        public void DerivedMethod()
        {
            Foo();
        }
    }
}

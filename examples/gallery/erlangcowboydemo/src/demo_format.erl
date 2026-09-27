-module(demo_format).

-export([banner/0, heading/1, subheading/1, explain/1, code/1,
         cowboy_box/2, success/1, warn/1, info/1, dim/1,
         separator/0, pause/0, nl/0]).

%% ANSI codes
-define(RESET,   "\e[0m").
-define(BOLD,    "\e[1m").
-define(DIM,     "\e[2m").
-define(CYAN,    "\e[36m").
-define(GREEN,   "\e[32m").
-define(YELLOW,  "\e[33m").
-define(MAGENTA, "\e[35m").
-define(WHITE,   "\e[37m").
-define(BG_BLUE, "\e[44m").

banner() ->
    io:format("~n"),
    io:format("~s~s", [?BOLD ++ ?CYAN, ""]),
    io:format("  ╔══════════════════════════════════════════════════╗~n"),
    io:format("  ║     Erlang/OTP Fundamentals for Cowboy Team      ║~n"),
    io:format("  ║         Interactive CLI Demo (v0.2.0)            ║~n"),
    io:format("  ╚══════════════════════════════════════════════════╝~n"),
    io:format("~s~n", [?RESET]),
    io:format("  Each lesson runs ~slive Erlang code~s, then maps~n", [?BOLD, ?RESET]),
    io:format("  the concept to its Cowboy blockchain equivalent.~n~n"),
    io:format("  ~n"),
    io:format("  It's recommended to open up cursor and look at the~n"),
    io:format("  code for each demo while running them!~n"),
    io:format("  ~n").

heading(Text) ->
    io:format("~n~s~s ═══ ~s ═══ ~s~n~n", [?BOLD, ?CYAN, Text, ?RESET]).

subheading(Text) ->
    io:format("~n  ~s~s▸ ~s~s~n", [?BOLD, ?WHITE, Text, ?RESET]).

explain(Text) ->
    io:format("  ~s~n", [Text]).

code(Text) ->
    io:format("    ~s~s~s~s~n", [?DIM, ?GREEN, Text, ?RESET]).

cowboy_box(ErlangConcept, CowboyEquivalent) ->
    io:format("~n  ~s~s┌─ Cowboy Mapping ─────────────────────────────┐~s~n", [?BOLD, ?MAGENTA, ?RESET]),
    io:format("  ~s│  Erlang: ~s~s~n", [?MAGENTA, ErlangConcept, ?RESET]),
    io:format("  ~s│  Cowboy: ~s~s~n", [?MAGENTA, CowboyEquivalent, ?RESET]),
    io:format("  ~s~s└──────────────────────────────────────────────┘~s~n", [?BOLD, ?MAGENTA, ?RESET]).

success(Text) ->
    io:format("  ~s~s✓ ~s~s~n", [?BOLD, ?GREEN, Text, ?RESET]).

warn(Text) ->
    io:format("  ~s~s⚠ ~s~s~n", [?BOLD, ?YELLOW, Text, ?RESET]).

info(Text) ->
    io:format("  ~s→ ~s~s~n", [?CYAN, Text, ?RESET]).

dim(Text) ->
    io:format("  ~s~s~s~n", [?DIM, Text, ?RESET]).

separator() ->
    io:format("~n  ~s────────────────────────────────────────────────~s~n", [?DIM, ?RESET]).

pause() ->
    io:format("~n  ~s[Press Enter to continue]~s ", [?DIM, ?RESET]),
    io:get_line(""),
    ok.

nl() ->
    io:format("~n").

-module(demo_cowboy_map).

-export([show_mapping/2, show_full_table/0]).

show_mapping(ErlangConcept, CowboyEquivalent) ->
    demo_format:cowboy_box(ErlangConcept, CowboyEquivalent).

show_full_table() ->
    demo_format:heading("Complete Erlang -> Cowboy Mapping"),
    demo_format:explain("Every Erlang/OTP concept has a direct counterpart in Cowboy's"),
    demo_format:explain("actor-model blockchain architecture:"),
    demo_format:nl(),
    Mappings = [
        {"Processes",
         "Actors",
         "Lightweight isolated units with an address"},
        {"Mailboxes",
         "Actor Mailboxes",
         "Async message queues, backpressure matters"},
        {"Process State",
         "Actor Persistent Storage",
         "Private key-value store per actor"},
        {"Message Passing (!)",
         "send_message()",
         "Async inter-actor messages, data is copied"},
        {"Reductions",
         "Cycles (Metered Computation)",
         "Budgeted execution per instruction"},
        {"Let-It-Crash",
         "Fail-Fast + Protocol Recovery",
         "Crash on invalid data, protocol restarts"},
        {"gen_server",
         "Actor Base Pattern",
         "State + message handler callbacks"},
        {"Supervisors",
         "Validator/Protocol-Level Restart",
         "Restart policies, fault containment"},
        {"Applications",
         "Deployed Actor Collections",
         "Protocol components, packaged services"}
    ],
    lists:foreach(
        fun({Erlang, Cowboy, Description}) ->
            PaddedErlang = string:pad(Erlang, 22, trailing),
            PaddedCowboy = string:pad(Cowboy, 32, trailing),
            io:format("  \e[1m\e[36m~ts\e[0m  ->  \e[1m\e[33m~ts\e[0m~n",
                      [PaddedErlang, PaddedCowboy]),
            io:format("  \e[2m  ~s\e[0m~n", [Description])
        end,
        Mappings),
    demo_format:nl(),
    demo_format:explain("Cowboy's actor model is a direct descendant of Erlang's process model."),
    demo_format:explain("The concepts are not just analogous -- they share the same lineage."),
    demo_format:nl().

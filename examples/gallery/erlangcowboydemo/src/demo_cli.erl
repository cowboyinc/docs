-module(demo_cli).

-export([main/1]).

main(_Args) ->
    demo_format:banner(),
    menu_loop().

menu_loop() ->
    show_menu(),
    case io:get_line("  Enter choice: ") of
        eof -> bye();
        {error, _} -> bye();
        Line ->
            Choice = string:trim(Line),
            handle_choice(Choice)
    end.

show_menu() ->
    io:format("  \e[1m\e[36mLessons:\e[0m~n"),
    io:format("    1  Processes~n"),
    io:format("    2  Mailboxes~n"),
    io:format("    3  Local State~n"),
    io:format("    4  Message Passing~n"),
    io:format("    5  Reductions~n"),
    io:format("    6  Crash-Fast / Let It Crash~n"),
    io:format("    7  gen_server~n"),
    io:format("    8  Supervisors~n"),
    io:format("    9  Applications~n"),
    io:format("~n"),
    io:format("  \e[1m\e[36mOther:\e[0m~n"),
    io:format("    a  Run ALL lessons (1-9)~n"),
    io:format("    m  Show full Erlang -> Cowboy mapping~n"),
    io:format("    q  Quit~n"),
    io:format("~n").

handle_choice("1") -> run_lesson(1);
handle_choice("2") -> run_lesson(2);
handle_choice("3") -> run_lesson(3);
handle_choice("4") -> run_lesson(4);
handle_choice("5") -> run_lesson(5);
handle_choice("6") -> run_lesson(6);
handle_choice("7") -> run_lesson(7);
handle_choice("8") -> run_lesson(8);
handle_choice("9") -> run_lesson(9);
handle_choice("a") -> run_all();
handle_choice("A") -> run_all();
handle_choice("m") -> show_mapping();
handle_choice("M") -> show_mapping();
handle_choice("q") -> bye();
handle_choice("Q") -> bye();
handle_choice(_) ->
    io:format("~n  \e[33mInvalid choice. Try 1-9, a, m, or q.\e[0m~n~n"),
    menu_loop().

run_lesson(N) ->
    demo_lessons:run(N),
    demo_format:separator(),
    demo_format:pause(),
    menu_loop().

run_all() ->
    lists:foreach(
        fun(N) ->
            demo_lessons:run(N),
            demo_format:separator(),
            demo_format:pause()
        end,
        lists:seq(1, 9)),
    demo_cowboy_map:show_full_table(),
    demo_format:pause(),
    menu_loop().

show_mapping() ->
    demo_cowboy_map:show_full_table(),
    demo_format:pause(),
    menu_loop().

bye() ->
    io:format("~n  \e[1m\e[36mThanks for exploring Erlang/OTP!"),
    io:format(" Now go build on Cowboy.\e[0m~n~n"),
    halt(0).

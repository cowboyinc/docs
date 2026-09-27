-module(demo_lessons).

-export([run/1]).

%%====================================================================
%% Public API
%%====================================================================

run(1) -> lesson_processes();
run(2) -> lesson_mailboxes();
run(3) -> lesson_state();
run(4) -> lesson_message_passing();
run(5) -> lesson_reductions();
run(6) -> lesson_crash();
run(7) -> lesson_gen_server();
run(8) -> lesson_supervisors();
run(9) -> lesson_applications().

%%====================================================================
%% Lesson 1: Processes
%%====================================================================

lesson_processes() ->
    demo_format:heading("Lesson 1: Processes"),
    demo_format:explain("Erlang processes are ultra-lightweight (< 1KB each), isolated,"),
    demo_format:explain("and managed by the BEAM VM -- not the OS. They share nothing."),

    %% Demo 1: Spawn 10 processes
    demo_format:subheading("Spawning 10 processes"),
    demo_format:code("Pids = [spawn(fun() -> receive stop -> ok end end) || _ <- lists:seq(1, 10)]"),
    Pids = [spawn(fun() -> receive stop -> ok end end) || _ <- lists:seq(1, 10)],
    lists:foreach(
        fun(Pid) ->
            demo_format:info(io_lib:format("PID ~p  alive=~p", [Pid, is_process_alive(Pid)]))
        end,
        Pids),
    lists:foreach(fun(P) -> P ! stop end, Pids),
    demo_format:success("10 processes spawned and stopped cleanly"),

    demo_format:pause(),

    %% Demo 2: Spawn 100K processes
    demo_format:subheading("Spawning 100,000 processes"),
    demo_format:code("[spawn(fun() -> ok end) || _ <- lists:seq(1, 100000)]"),
    T1 = erlang:monotonic_time(microsecond),
    _BigList = [spawn(fun() -> ok end) || _ <- lists:seq(1, 100000)],
    T2 = erlang:monotonic_time(microsecond),
    Elapsed = T2 - T1,
    PerProc = Elapsed / 100000,
    demo_format:success(io_lib:format("100K processes in ~.1f ms (~.2f us each)",
                                       [Elapsed / 1000.0, PerProc])),

    demo_format:pause(),

    %% Demo 3: Process isolation
    demo_format:subheading("Process isolation (separate dictionaries)"),
    demo_format:code("spawn(fun() -> put(key, alice), Parent ! {self(), get(key)} end)"),
    Self = self(),
    put(key, parent_value),
    P1 = spawn(fun() -> put(key, alice), Self ! {self(), get(key)} end),
    P2 = spawn(fun() -> put(key, bob), Self ! {self(), get(key)} end),
    receive {P1, V1} -> demo_format:info(io_lib:format("Process 1 sees key = ~p", [V1])) end,
    receive {P2, V2} -> demo_format:info(io_lib:format("Process 2 sees key = ~p", [V2])) end,
    demo_format:info(io_lib:format("Parent still sees key = ~p", [get(key)])),
    erase(key),
    demo_format:success("Each process has its own isolated dictionary"),

    demo_cowboy_map:show_mapping(
        "Processes -- lightweight, isolated, garbage-collected independently",
        "Actors -- independent entities with address, state, and mailbox").

%%====================================================================
%% Lesson 2: Mailboxes
%%====================================================================

lesson_mailboxes() ->
    demo_format:heading("Lesson 2: Mailboxes"),
    demo_format:explain("Every process has a mailbox -- an unbounded FIFO queue."),
    demo_format:explain("Messages accumulate until the process reads them with 'receive'."),

    %% Demo 1: Queue depth
    demo_format:subheading("Sending 1000 messages to a non-reading process"),
    demo_format:code("Pid ! {msg, N}  %% 1000 times, process never reads"),
    Sleeper = spawn(fun() -> receive never -> ok end end),
    lists:foreach(fun(N) -> Sleeper ! {msg, N} end, lists:seq(1, 1000)),
    {message_queue_len, Len} = process_info(Sleeper, message_queue_len),
    demo_format:success(io_lib:format("Mailbox depth: ~p messages (unread)", [Len])),
    exit(Sleeper, kill),

    demo_format:pause(),

    %% Demo 2: Selective receive
    demo_format:subheading("Selective receive with pattern matching"),
    demo_format:explain("Erlang's 'receive' can pick specific messages from the mailbox,"),
    demo_format:explain("leaving non-matching messages in place."),
    demo_format:code("receive {temperature, T} -> T end  %% skips humidity messages"),
    Self = self(),
    spawn(fun() ->
        Self ! {humidity, 65},
        Self ! {pressure, 1013},
        Self ! {temperature, 22},
        Self ! {humidity, 70},
        Self ! {temperature, 25}
    end),
    timer:sleep(50),
    Temps = collect_temps([], 2),
    demo_format:info(io_lib:format("Picked temperatures: ~p (skipped humidity & pressure)", [Temps])),
    flush_mailbox(),
    demo_format:success("Selective receive lets you prioritize message types"),

    demo_format:pause(),

    %% Demo 3: Slow consumer
    demo_format:subheading("Slow consumer: mailbox grows when receiver is slow"),
    demo_format:code("Slow receiver sleeps 50ms per message, sender floods 20 msgs instantly"),
    SlowPid = spawn(fun() -> slow_consumer(0) end),
    lists:foreach(fun(N) -> SlowPid ! {work, N} end, lists:seq(1, 20)),
    timer:sleep(10),
    {message_queue_len, SlowLen} = process_info(SlowPid, message_queue_len),
    demo_format:info(io_lib:format("Immediately after send: ~p msgs queued", [SlowLen])),
    timer:sleep(300),
    case is_process_alive(SlowPid) of
        true ->
            {message_queue_len, SlowLen2} = process_info(SlowPid, message_queue_len),
            demo_format:info(io_lib:format("After 300ms: ~p msgs still queued", [SlowLen2]));
        false ->
            demo_format:info("Consumer finished")
    end,
    exit(SlowPid, kill),
    demo_format:warn("Unbounded mailboxes can cause memory issues -- backpressure matters!"),

    demo_cowboy_map:show_mapping(
        "Mailboxes -- unbounded FIFO queue per process",
        "Actor mailbox -- message queue, backpressure is critical").

%%====================================================================
%% Lesson 3: Local State
%%====================================================================

lesson_state() ->
    demo_format:heading("Lesson 3: Local State"),
    demo_format:explain("Processes maintain state in two ways: the process dictionary"),
    demo_format:explain("(mutable key-value store) or recursive function arguments (idiomatic)."),

    %% Demo 1: Process dictionary
    demo_format:subheading("Process dictionary (put/get)"),
    demo_format:code("put(balance, 100), get(balance)"),
    put(balance, 100),
    demo_format:info(io_lib:format("put(balance, 100) -> get(balance) = ~p", [get(balance)])),
    put(balance, get(balance) + 50),
    demo_format:info(io_lib:format("After +50: balance = ~p", [get(balance)])),
    erase(balance),
    demo_format:dim("(Process dictionary is mutable -- generally avoided in production)"),

    demo_format:pause(),

    %% Demo 2: State via recursion
    demo_format:subheading("State via recursive function arguments (idiomatic Erlang)"),
    demo_format:code("loop(State) -> receive {add, V} -> loop(State + V) end"),
    Self = self(),
    Counter = spawn(fun() -> state_loop(Self, 0) end),
    Counter ! {add, 10},
    Counter ! {add, 25},
    Counter ! {add, 7},
    Counter ! {get, Self},
    receive {state, Val} -> demo_format:info(io_lib:format("After +10, +25, +7: state = ~p", [Val])) end,
    Counter ! stop,
    demo_format:success("Immutable state threaded through recursion -- no mutation needed"),

    demo_format:pause(),

    %% Demo 3: Isolation proof
    demo_format:subheading("State isolation: two processes, same key, different values"),
    demo_format:code("Two processes both put(color, ...) -- each sees only their own"),
    Parent = self(),
    spawn(fun() -> put(color, red), timer:sleep(50), Parent ! {color, get(color)} end),
    spawn(fun() -> put(color, blue), timer:sleep(50), Parent ! {color, get(color)} end),
    receive {color, C1} -> demo_format:info(io_lib:format("Process A: color = ~p", [C1])) end,
    receive {color, C2} -> demo_format:info(io_lib:format("Process B: color = ~p", [C2])) end,
    demo_format:success("Complete isolation -- no shared mutable state"),

    demo_cowboy_map:show_mapping(
        "Process state -- recursive args or process dictionary",
        "Actor persistent storage -- private key-value store per actor").

%%====================================================================
%% Lesson 4: Message Passing
%%====================================================================

lesson_message_passing() ->
    demo_format:heading("Lesson 4: Message Passing"),
    demo_format:explain("The ! operator sends a message asynchronously. Messages are always"),
    demo_format:explain("copied -- the sender retains its original data."),

    %% Demo 1: Request/reply
    demo_format:subheading("Request/reply pattern"),
    demo_format:code("Server ! {self(), square, 7}, receive {reply, R} -> R end"),
    Self = self(),
    Server = spawn(fun() -> math_server() end),
    Server ! {Self, square, 7},
    receive {reply, Result} ->
        demo_format:info(io_lib:format("square(7) = ~p", [Result]))
    end,
    Server ! {Self, square, 12},
    receive {reply, Result2} ->
        demo_format:info(io_lib:format("square(12) = ~p", [Result2]))
    end,
    Server ! stop,
    demo_format:success("Request/reply via message passing -- no shared memory"),

    demo_format:pause(),

    %% Demo 2: Fan-out
    demo_format:subheading("Fan-out: broadcast to 5 workers, collect in completion order"),
    demo_format:code("Broadcast work to 5 processes, results arrive as they finish"),
    Parent = self(),
    Workers = lists:map(
        fun(Id) ->
            spawn(fun() ->
                Delay = rand:uniform(200),
                timer:sleep(Delay),
                Parent ! {result, Id, Delay}
            end)
        end,
        lists:seq(1, 5)),
    demo_format:info(io_lib:format("Spawned ~p workers with random delays", [length(Workers)])),
    Results = collect_results([], 5),
    lists:foreach(
        fun({Id, Delay}) ->
            demo_format:info(io_lib:format("Worker ~p finished in ~pms", [Id, Delay]))
        end,
        Results),
    demo_format:success("Results arrive in completion order, not send order"),

    demo_format:pause(),

    %% Demo 3: Messages are copies
    demo_format:subheading("Messages are copies: sender retains original"),
    demo_format:code("Sender modifies list after sending -- receiver gets the original"),
    Receiver = spawn(fun() ->
        receive {data, Data} ->
            Parent ! {received, Data}
        end
    end),
    OriginalData = [1, 2, 3, 4, 5],
    Receiver ! {data, OriginalData},
    ModifiedData = [99 | OriginalData],
    demo_format:info(io_lib:format("Sender's modified list: ~p", [ModifiedData])),
    receive {received, RecvData} ->
        demo_format:info(io_lib:format("Receiver got original:  ~p", [RecvData]))
    end,
    demo_format:success("Data is copied on send -- no aliasing, no races"),

    demo_cowboy_map:show_mapping(
        "Message passing (!) -- async, copied data, no shared memory",
        "send_message() -- async inter-actor messages, data is copied").

%%====================================================================
%% Lesson 5: Reductions
%%====================================================================

lesson_reductions() ->
    demo_format:heading("Lesson 5: Reductions"),
    demo_format:explain("The BEAM scheduler tracks 'reductions' -- a unit of work per process."),
    demo_format:explain("After ~4000 reductions, a process yields. This ensures fair scheduling."),

    %% Demo 1: Measure reductions
    demo_format:subheading("Reduction cost of common operations"),
    demo_format:code("{reductions, R0} = process_info(self(), reductions)"),

    {reductions, R0} = process_info(self(), reductions),
    _L1 = lists:seq(1, 1000),
    {reductions, R1} = process_info(self(), reductions),
    demo_format:info(io_lib:format("lists:seq(1, 1000)        cost ~p reductions", [R1 - R0])),

    {reductions, R2} = process_info(self(), reductions),
    _L2 = [X * X || X <- lists:seq(1, 1000)],
    {reductions, R3} = process_info(self(), reductions),
    demo_format:info(io_lib:format("[X*X || X <- 1..1000]     cost ~p reductions", [R3 - R2])),

    {reductions, R4} = process_info(self(), reductions),
    _Sum = lists:foldl(fun(X, Acc) -> X + Acc end, 0, lists:seq(1, 1000)),
    {reductions, R5} = process_info(self(), reductions),
    demo_format:info(io_lib:format("foldl sum over 1..1000   cost ~p reductions", [R5 - R4])),

    demo_format:pause(),

    %% Demo 2: Fair scheduling
    demo_format:subheading("Fair scheduling: CPU hog doesn't starve quick process"),
    demo_format:code("Hog does heavy work; Quick does light work -- both make progress"),
    Self = self(),
    T0 = erlang:monotonic_time(millisecond),
    _Hog = spawn(fun() ->
        lists:foldl(fun(X, Acc) -> X + Acc end, 0, lists:seq(1, 1000000)),
        Self ! {done, hog, erlang:monotonic_time(millisecond) - T0}
    end),
    _Quick = spawn(fun() ->
        lists:sum(lists:seq(1, 100)),
        Self ! {done, quick, erlang:monotonic_time(millisecond) - T0}
    end),
    receive {done, quick, QTime} ->
        demo_format:info(io_lib:format("Quick process finished in ~pms", [QTime]))
    end,
    receive {done, hog, HTime} ->
        demo_format:info(io_lib:format("Hog process finished in ~pms", [HTime]))
    end,
    demo_format:success("Quick process wasn't starved -- preemptive scheduling works"),

    demo_format:pause(),

    %% Demo 3: Scheduler info
    demo_format:subheading("BEAM scheduler information"),
    SchedCount = erlang:system_info(schedulers),
    OnlineCount = erlang:system_info(schedulers_online),
    demo_format:info(io_lib:format("Schedulers configured: ~p", [SchedCount])),
    demo_format:info(io_lib:format("Schedulers online:     ~p", [OnlineCount])),
    demo_format:info(io_lib:format("Logical CPUs:          ~p", [erlang:system_info(logical_processors)])),
    demo_format:explain("One scheduler per CPU core -- processes are multiplexed across them."),

    demo_cowboy_map:show_mapping(
        "Reductions -- budgeted execution, preemptive fair scheduling",
        "Cycles -- metered computation budget per instruction").

%%====================================================================
%% Lesson 6: Crash-Fast / Let It Crash
%%====================================================================

lesson_crash() ->
    demo_format:heading("Lesson 6: Crash-Fast / Let It Crash"),
    demo_format:explain("In Erlang, processes are expected to crash on unexpected input."),
    demo_format:explain("Other processes detect the crash and handle recovery."),

    %% Suppress error logger noise for intentional crashes
    OldLevel = logger:get_primary_config(),
    logger:set_primary_config(level, none),

    %% Demo 1: Process crash, parent survives
    demo_format:subheading("Process crash detected via monitor"),
    demo_format:code("Ref = monitor(process, spawn(fun() -> error(bad_data) end))"),
    {CrashPid, Ref} = spawn_monitor(fun() ->
        timer:sleep(100),
        error(something_went_wrong)
    end),
    demo_format:info(io_lib:format("Spawned ~p with monitor", [CrashPid])),
    receive
        {'DOWN', Ref, process, CrashPid, Reason} ->
            {ErrorAtom, _Stack} = Reason,
            demo_format:info(io_lib:format("Got DOWN: reason = ~p", [ErrorAtom])),
            demo_format:success("Parent process survived the child crash!")
    end,

    demo_format:pause(),

    %% Demo 2: crash_worker gen_server (unlinked so crash doesn't kill us)
    demo_format:subheading("crash_worker: valid txn works, invalid crashes the server"),
    demo_format:code("crash_worker:process_txn(Pid, #{type => transfer, amount => 100})"),
    {ok, Worker} = crash_worker:start(),
    WRef = monitor(process, Worker),
    Result1 = crash_worker:process_txn(Worker, #{type => transfer, amount => 100}),
    demo_format:info(io_lib:format("Valid txn result: ~p", [Result1])),
    demo_format:info("Now sending invalid transaction (negative amount)..."),
    catch crash_worker:process_txn(Worker, #{type => transfer, amount => -50}),
    receive
        {'DOWN', WRef, process, Worker, CrashReason} ->
            {CrashAtom, _CrashStack} = CrashReason,
            demo_format:info(io_lib:format("Worker crashed: ~p", [CrashAtom])),
            demo_format:success("Crash-fast: invalid data causes immediate failure")
    after 1000 ->
        demo_format:warn("Worker didn't crash (unexpected)")
    end,

    demo_format:pause(),

    %% Demo 3: Links vs monitors
    demo_format:subheading("Links vs Monitors"),
    demo_format:explain("  Links:    bidirectional, crash propagates to linked process"),
    demo_format:explain("  Monitors: unidirectional, observer gets a DOWN message"),
    demo_format:nl(),
    demo_format:code("link(Pid)            -- crash kills both (unless trapping exits)"),
    demo_format:code("monitor(process, Pid) -- observer gets {'DOWN', ...} message"),
    demo_format:nl(),
    %% Quick demo of monitor
    {MonPid, MonRef} = spawn_monitor(fun() -> timer:sleep(50), exit(normal) end),
    receive
        {'DOWN', MonRef, process, MonPid, normal} ->
            demo_format:info("Monitor: got DOWN with reason 'normal' -- observer unaffected")
    end,
    demo_format:success("Monitors are safer -- observer always survives"),

    %% Restore error logger
    #{level := OrigLevel} = OldLevel,
    logger:set_primary_config(level, OrigLevel),

    demo_cowboy_map:show_mapping(
        "Let-it-crash -- fail fast on bad data, supervisors recover",
        "Fail-fast on invalid data, protocol-level recovery").

%%====================================================================
%% Lesson 7: gen_server
%%====================================================================

lesson_gen_server() ->
    demo_format:heading("Lesson 7: gen_server"),
    demo_format:explain("gen_server is the standard OTP behaviour for stateful servers."),
    demo_format:explain("You implement callbacks; OTP handles the message loop."),

    %% Demo 1: Callback structure
    demo_format:subheading("gen_server callback structure"),
    demo_format:code("init/1          -- initialize state"),
    demo_format:code("handle_call/3   -- sync request/reply"),
    demo_format:code("handle_cast/2   -- async fire-and-forget"),
    demo_format:code("handle_info/2   -- raw messages (!, timers)"),
    demo_format:code("terminate/2     -- cleanup on shutdown"),

    demo_format:pause(),

    %% Demo 2: counter_server in action
    demo_format:subheading("counter_server in action"),
    demo_format:code("counter_server:start_link(demo_counter)"),
    {ok, _} = counter_server:start_link(demo_counter),
    C0 = counter_server:get_count(demo_counter),
    demo_format:info(io_lib:format("Initial count: ~p", [C0])),
    counter_server:increment(demo_counter),
    counter_server:increment(demo_counter),
    counter_server:increment(demo_counter),
    timer:sleep(50),
    C1 = counter_server:get_count(demo_counter),
    demo_format:info(io_lib:format("After 3 increments: ~p", [C1])),
    demo_format:success("gen_server manages state across calls"),

    demo_format:pause(),

    %% Demo 3: Raw message to gen_server
    demo_format:subheading("Sending raw message to gen_server (handle_info)"),
    demo_format:code("demo_counter ! {hello, from_outside}"),
    demo_counter ! {hello, from_outside},
    timer:sleep(100),
    demo_format:info("The gen_server's handle_info/2 caught that raw message"),
    counter_server:stop(demo_counter),

    demo_format:pause(),

    %% Demo 4: Reference random_worker
    demo_format:subheading("Real-world example: random_worker.erl"),
    demo_format:explain("Your existing random_worker is a gen_server that:"),
    demo_format:explain("  - Maintains state (name, peers, sent/received counts)"),
    demo_format:explain("  - Handles casts ({ping, From})"),
    demo_format:explain("  - Uses handle_info for timer ticks"),
    demo_format:explain("  - Provides stats/1 via handle_call"),
    demo_format:code("See: src/random_worker.erl"),

    demo_cowboy_map:show_mapping(
        "gen_server -- state + callbacks (call/cast/info)",
        "Actor base pattern -- state + message handler callbacks").

%%====================================================================
%% Lesson 8: Supervisors
%%====================================================================

lesson_supervisors() ->
    demo_format:heading("Lesson 8: Supervisors"),
    demo_format:explain("Supervisors monitor child processes and restart them on crash."),
    demo_format:explain("This is the heart of OTP's fault tolerance."),

    %% Suppress error logger noise for intentional crashes
    #{level := SupOrigLevel} = logger:get_primary_config(),
    logger:set_primary_config(level, none),

    %% Demo 1: Start supervisor with two children
    demo_format:subheading("Starting a supervisor with two counter_server children"),
    demo_format:code("demo_sup_helper:start_link([counter_a, counter_b])"),
    process_flag(trap_exit, true),
    {ok, SupPid} = demo_sup_helper:start_link([counter_a, counter_b]),
    Children = supervisor:which_children(SupPid),
    lists:foreach(
        fun({Id, Pid, _Type, _Mods}) ->
            demo_format:info(io_lib:format("Child ~p -> ~p (alive=~p)", [Id, Pid, is_process_alive(Pid)]))
        end,
        Children),
    demo_format:success("Supervisor started with 2 children"),

    demo_format:pause(),

    %% Demo 2: Crash one child, watch it restart
    demo_format:subheading("Crashing counter_a -- supervisor will restart it"),
    counter_server:increment(counter_a),
    counter_server:increment(counter_a),
    C_Before = counter_server:get_count(counter_a),
    demo_format:info(io_lib:format("counter_a count before crash: ~p", [C_Before])),
    OldPid = whereis(counter_a),
    demo_format:code("exit(whereis(counter_a), kill)"),
    exit(OldPid, kill),
    timer:sleep(200),
    NewPid = whereis(counter_a),
    demo_format:info(io_lib:format("Old PID: ~p", [OldPid])),
    demo_format:info(io_lib:format("New PID: ~p (restarted!)", [NewPid])),
    C_After = counter_server:get_count(counter_a),
    demo_format:info(io_lib:format("counter_a count after restart: ~p (fresh state)", [C_After])),
    demo_format:success("Supervisor detected crash and restarted with fresh state"),

    demo_format:pause(),

    %% Demo 3: Sibling unaffected
    demo_format:subheading("one_for_one: sibling counter_b is unaffected"),
    counter_server:increment(counter_b),
    CB = counter_server:get_count(counter_b),
    demo_format:info(io_lib:format("counter_b count: ~p (untouched by counter_a's crash)", [CB])),
    demo_format:success("one_for_one: only the crashed child restarts"),

    demo_format:pause(),

    %% Explain strategies
    demo_format:subheading("Restart strategies"),
    demo_format:explain("  one_for_one:  only restart the crashed child"),
    demo_format:explain("  one_for_all:  restart ALL children if one crashes"),
    demo_format:explain("  rest_for_one: restart the crashed child + all children started after it"),
    demo_format:nl(),
    demo_format:explain("  {one_for_one, MaxRestarts, TimeWindow}"),
    demo_format:explain("  e.g., {one_for_one, 5, 10} = max 5 restarts in 10 seconds"),

    %% Cleanup
    exit(SupPid, shutdown),
    timer:sleep(100),
    process_flag(trap_exit, false),
    flush_mailbox(),

    %% Restore error logger
    logger:set_primary_config(level, SupOrigLevel),

    demo_cowboy_map:show_mapping(
        "Supervisors -- restart strategies, fault containment",
        "Protocol-level restart/recovery, validator management").

%%====================================================================
%% Lesson 9: Applications
%%====================================================================

lesson_applications() ->
    demo_format:heading("Lesson 9: Applications"),
    demo_format:explain("An OTP application packages modules, a supervision tree, and config"),
    demo_format:explain("into a deployable unit. It's how you ship Erlang software."),

    %% Demo 1: .app.src structure
    demo_format:subheading("Application resource file (.app.src)"),
    demo_format:code("{application, erlangcowboydemo, ["),
    demo_format:code("  {description, \"...\"},"),
    demo_format:code("  {vsn, \"0.2.0\"},"),
    demo_format:code("  {mod, {erlangcowboydemo_app, []}},  %% start callback"),
    demo_format:code("  {applications, [kernel, stdlib]},    %% dependencies"),
    demo_format:code("  {env, [{workers, [alpha, bravo, ...]}]}"),
    demo_format:code("]}."),
    demo_format:nl(),
    demo_format:explain("  mod:          the module that implements application behaviour"),
    demo_format:explain("  applications: apps that must start before this one"),
    demo_format:explain("  env:          application-level config (accessed via get_env)"),

    demo_format:pause(),

    %% Demo 2: Start the application
    demo_format:subheading("Starting the erlangcowboydemo application"),
    demo_format:code("application:ensure_all_started(erlangcowboydemo)"),
    case application:ensure_all_started(erlangcowboydemo) of
        {ok, Started} ->
            demo_format:success(io_lib:format("Started applications: ~p", [Started]));
        {error, Reason} ->
            demo_format:warn(io_lib:format("Could not start: ~p", [Reason]))
    end,

    demo_format:pause(),

    %% Demo 3: Inspect supervision tree
    demo_format:subheading("Inspecting the supervision tree"),
    case whereis(erlangcowboydemo_sup) of
        undefined ->
            demo_format:warn("Supervisor not running");
        SupPid ->
            demo_format:info(io_lib:format("Supervisor PID: ~p", [SupPid])),
            AppChildren = supervisor:which_children(SupPid),
            lists:foreach(
                fun({Id, Pid, Type, _Mods}) ->
                    demo_format:info(io_lib:format("  ~p -> ~p (~p)", [Id, Pid, Type]))
                end,
                AppChildren),
            demo_format:success("Supervision tree is live")
    end,

    demo_format:pause(),

    %% Demo 4: Query worker stats
    demo_format:subheading("Querying worker stats"),
    {ok, WorkerNames} = application:get_env(erlangcowboydemo, workers),
    demo_format:info(io_lib:format("Configured workers: ~p", [WorkerNames])),
    timer:sleep(2000),
    demo_format:explain("After letting workers run for 2 seconds:"),
    lists:foreach(
        fun(Name) ->
            case catch random_worker:stats(Name) of
                {Sent, Received} ->
                    demo_format:info(io_lib:format("  ~p: sent=~p received=~p", [Name, Sent, Received]));
                _ ->
                    demo_format:dim(io_lib:format("  ~p: not responding", [Name]))
            end
        end,
        WorkerNames),

    demo_format:pause(),

    %% Demo 5: application:get_env
    demo_format:subheading("Application config via get_env"),
    demo_format:code("application:get_env(erlangcowboydemo, workers)"),
    {ok, Ws} = application:get_env(erlangcowboydemo, workers),
    demo_format:info(io_lib:format("workers = ~p", [Ws])),
    {ok, Ss} = application:get_env(erlangcowboydemo, sinks),
    demo_format:info(io_lib:format("sinks   = ~p", [Ss])),

    demo_format:pause(),

    %% Demo 6: Stop the application
    demo_format:subheading("Stopping the application"),
    demo_format:code("application:stop(erlangcowboydemo)"),
    #{level := AppOrigLevel} = logger:get_primary_config(),
    logger:set_primary_config(level, none),
    application:stop(erlangcowboydemo),
    timer:sleep(100),
    logger:set_primary_config(level, AppOrigLevel),
    demo_format:success("Application stopped cleanly"),
    demo_format:info("All workers and supervisors have been terminated"),

    demo_cowboy_map:show_mapping(
        "Applications -- packaged modules, supervision tree, config",
        "Deployed actor collections, protocol components").

%%====================================================================
%% Internal helpers
%%====================================================================

collect_temps(Acc, 0) -> lists:reverse(Acc);
collect_temps(Acc, N) ->
    receive
        {temperature, T} -> collect_temps([T | Acc], N - 1)
    after 500 -> lists:reverse(Acc)
    end.

flush_mailbox() ->
    receive _ -> flush_mailbox()
    after 0 -> ok
    end.

slow_consumer(Count) ->
    receive
        {work, _N} ->
            timer:sleep(50),
            slow_consumer(Count + 1);
        _ ->
            slow_consumer(Count)
    after 2000 ->
        ok
    end.

state_loop(Parent, State) ->
    receive
        {add, V} ->
            state_loop(Parent, State + V);
        {get, From} ->
            From ! {state, State},
            state_loop(Parent, State);
        stop ->
            ok
    end.

math_server() ->
    receive
        {From, square, N} ->
            From ! {reply, N * N},
            math_server();
        stop ->
            ok
    end.

collect_results(Acc, 0) -> lists:reverse(Acc);
collect_results(Acc, N) ->
    receive
        {result, Id, Delay} -> collect_results([{Id, Delay} | Acc], N - 1)
    after 5000 -> lists:reverse(Acc)
    end.

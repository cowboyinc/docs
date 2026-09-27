-module(mailbox_accumulator).
-behaviour(gen_server).

-export([start_link/1, stats/1]).
-export([init/1, handle_call/3, handle_cast/2, handle_info/2, terminate/2, code_change/3]).

-define(SLEEP_MS, 3000).

-record(state, {
    name,
    received = 0
}).

start_link(Name) ->
    gen_server:start_link({local, Name}, ?MODULE, Name, []).

stats(Name) ->
    gen_server:call(Name, stats).

init(Name) ->
    process_flag(trap_exit, true),
    {ok, #state{name = Name}}.

handle_call(stats, _From, State = #state{}) ->
    {reply, {State#state.received}, State};
handle_call(_Req, _From, State) ->
    {reply, ok, State}.

handle_cast({ping, From}, State = #state{name = Name, received = R}) ->
    %% Intentional slow handling to let mailbox grow
    timer:sleep(?SLEEP_MS),
    io:format("~p slowly consumed ping from ~p~n", [Name, From]),
    {noreply, State#state{received = R + 1}};
handle_cast(_Msg, State) ->
    {noreply, State}.

handle_info(_Info, State) ->
    {noreply, State}.

terminate(_Reason, _State) ->
    ok.

code_change(_OldVsn, State, _Extra) ->
    {ok, State}.

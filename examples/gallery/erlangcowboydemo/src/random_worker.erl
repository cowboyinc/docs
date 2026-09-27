-module(random_worker).
-behaviour(gen_server).

-export([start_link/2, stats/1]).
-export([init/1, handle_call/3, handle_cast/2, handle_info/2, terminate/2, code_change/3]).

-define(MIN_MS, 800).
-define(MAX_MS, 2500).

-record(state, {
    name,
    peers = [],
    sent = 0,
    received = 0
}).

start_link(Name, AllNames) ->
    gen_server:start_link({local, Name}, ?MODULE, {Name, AllNames}, []).

stats(Name) ->
    gen_server:call(Name, stats).

init({Name, AllNames}) ->
    process_flag(trap_exit, true),
    rand:seed(exsplus, seed_tuple()),
    Peers = [N || N <- AllNames, N =/= Name],
    schedule_tick(),
    {ok, #state{name = Name, peers = Peers}}.

handle_call(stats, _From, State = #state{}) ->
    {reply, {State#state.sent, State#state.received}, State};
handle_call(_Req, _From, State) ->
    {reply, ok, State}.

handle_cast({ping, From}, State = #state{name = Name, received = R}) ->
    io:format("~p got ping from ~p~n", [Name, From]),
    {noreply, State#state{received = R + 1}};
handle_cast(_Msg, State) ->
    {noreply, State}.

handle_info(tick, State = #state{name = Name, peers = Peers, sent = S}) ->
    maybe_send(Name, Peers),
    schedule_tick(),
    {noreply, State#state{sent = S + 1}};
handle_info(_Info, State) ->
    {noreply, State}.

terminate(_Reason, _State) ->
    ok.

code_change(_OldVsn, State, _Extra) ->
    {ok, State}.

maybe_send(_Name, []) ->
    ok;
maybe_send(Name, Peers) ->
    Peer = random_peer(Peers),
    gen_server:cast(Peer, {ping, Name}).

random_peer(Peers) ->
    Index = rand:uniform(length(Peers)),
    lists:nth(Index, Peers).

schedule_tick() ->
    Delay = ?MIN_MS + rand:uniform(?MAX_MS - ?MIN_MS + 1) - 1,
    erlang:send_after(Delay, self(), tick).

seed_tuple() ->
    {erlang:monotonic_time(),
     erlang:unique_integer([positive]),
     erlang:phash2(self())}.

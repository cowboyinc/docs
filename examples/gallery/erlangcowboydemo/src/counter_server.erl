-module(counter_server).
-behaviour(gen_server).

-export([start_link/1, start_link/2, increment/1, get_count/1, stop/1]).
-export([init/1, handle_call/3, handle_cast/2, handle_info/2, terminate/2]).

-record(state, {
    name :: atom(),
    count = 0 :: non_neg_integer()
}).

start_link(Name) ->
    start_link(Name, []).

start_link(Name, _Args) ->
    gen_server:start_link({local, Name}, ?MODULE, Name, []).

increment(Name) ->
    gen_server:cast(Name, increment).

get_count(Name) ->
    gen_server:call(Name, get_count).

stop(Name) ->
    gen_server:stop(Name).

%% gen_server callbacks

init(Name) ->
    {ok, #state{name = Name, count = 0}}.

handle_call(get_count, _From, State = #state{count = C}) ->
    {reply, C, State};
handle_call(_Req, _From, State) ->
    {reply, {error, unknown_call}, State}.

handle_cast(increment, State = #state{count = C}) ->
    {noreply, State#state{count = C + 1}};
handle_cast(_Msg, State) ->
    {noreply, State}.

handle_info(Msg, State = #state{name = Name}) ->
    io:format("    [~p] got unexpected message: ~p~n", [Name, Msg]),
    {noreply, State}.

terminate(_Reason, _State) ->
    ok.

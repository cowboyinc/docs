-module(crash_worker).
-behaviour(gen_server).

-export([start_link/0, start_link/1, start/0, process_txn/2, stop/1]).
-export([init/1, handle_call/3, handle_cast/2, handle_info/2, terminate/2]).

-record(state, {
    processed = 0 :: non_neg_integer()
}).

start_link() ->
    gen_server:start_link(?MODULE, [], []).

start() ->
    gen_server:start(?MODULE, [], []).

start_link(Name) ->
    gen_server:start_link({local, Name}, ?MODULE, [], []).

process_txn(Pid, Txn) ->
    gen_server:call(Pid, {process_txn, Txn}).

stop(Pid) ->
    gen_server:stop(Pid).

%% gen_server callbacks

init([]) ->
    {ok, #state{}}.

handle_call({process_txn, #{type := transfer, amount := Amt}}, _From, State)
  when is_number(Amt), Amt > 0 ->
    NewCount = State#state.processed + 1,
    {reply, {ok, NewCount}, State#state{processed = NewCount}};
handle_call({process_txn, #{type := transfer, amount := Amt}}, _From, _State)
  when is_number(Amt), Amt =< 0 ->
    error({invalid_amount, Amt});
handle_call({process_txn, BadTxn}, _From, _State) ->
    error({malformed_txn, BadTxn});
handle_call(_Req, _From, State) ->
    {reply, {error, unknown_call}, State}.

handle_cast(_Msg, State) ->
    {noreply, State}.

handle_info(_Info, State) ->
    {noreply, State}.

terminate(_Reason, _State) ->
    ok.

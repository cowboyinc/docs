-module(erlangcowboydemo_sup).
-behaviour(supervisor).

-export([start_link/0]).
-export([init/1]).

start_link() ->
    supervisor:start_link({local, ?MODULE}, ?MODULE, []).

init([]) ->
    {ok, WorkerNames} = application:get_env(erlangcowboydemo, workers),
    {ok, SinkNames} = application:get_env(erlangcowboydemo, sinks),
    AllNames = WorkerNames ++ SinkNames,
    RandomChildren = [random_child_spec(Name, AllNames) || Name <- WorkerNames],
    SinkChildren = [sink_child_spec(Name) || Name <- SinkNames],
    Children = RandomChildren ++ SinkChildren,
    {ok, {{one_for_one, 5, 10}, Children}}.

random_child_spec(Name, AllNames) ->
    #{id => Name,
      start => {random_worker, start_link, [Name, AllNames]},
      restart => permanent,
      shutdown => 5000,
      type => worker,
      modules => [random_worker]}.

sink_child_spec(Name) ->
    #{id => Name,
      start => {mailbox_accumulator, start_link, [Name]},
      restart => permanent,
      shutdown => 5000,
      type => worker,
      modules => [mailbox_accumulator]}.

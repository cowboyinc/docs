-module(demo_sup_helper).
-behaviour(supervisor).

-export([start_link/1, init/1]).

start_link(Children) ->
    supervisor:start_link(?MODULE, Children).

init(Children) ->
    ChildSpecs = [child_spec(Name) || Name <- Children],
    {ok, {{one_for_one, 5, 10}, ChildSpecs}}.

child_spec(Name) ->
    #{id => Name,
      start => {counter_server, start_link, [Name]},
      restart => permanent,
      shutdown => 5000,
      type => worker,
      modules => [counter_server]}.

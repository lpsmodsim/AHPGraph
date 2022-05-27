"""This module extends a DeviceGraph to enables SST Simulation output."""

import os
import gc
import orjson
from .DeviceGraph import *


class SSTGraph(DeviceGraph):
    """SSTGraph."""

    def __init__(self, graph: DeviceGraph) -> 'SSTGraph':
        """Define."""
        self.attr = graph.attr
        self.devices = graph.devices
        self.links = graph.links

    def build(self, nranks: int = 1) -> dict:
        """
        Build the SST graph.

        Return a dictionary of component names to SST component objects.
        If you have an extremely large graph, it is recommended that you use
        AHPGraph to do the graph partitioning instead of letting SST do it. You
        can do this by using the Device.set_partition() function and then
        setting nranks in this function to the total number of ranks used
        """
        # only import sst when we are going to build the graph
        import sst

        # If this is serial or sst is doing the partitioning,
        # generate the entire graph
        if nranks == 1:
            self = SSTGraph(self.flatten())
            self.verify_links()
            return self.__build_model(False)

        # Find the partition information and build the model for this rank
        else:
            sst.setProgramOption("partitioner", "sst.self")
            rank = sst.getMyMPIRank()
            # If this is an extra rank, don't do anything
            if rank >= nranks:
                return dict()

            self.check_partition()
            rankGraph = self.flatten(rank=rank)
            # need to verify before we follow links because pruning
            # may make the overall graph invalid
            # (it will be valid for the current rank)
            rankGraph.verify_links()
            rankGraph = SSTGraph(rankGraph.follow_links(rank, True))
            return rankGraph.__build_model(True)

    def write_json(self, filename: str, nranks: int = 1,
                   program_options: dict = None) -> None:
        """
        Generate the JSON and write it to the specified filename.

        All output will be stored in a folder called output

        The program_options dictionary provides a way to pass SST
        program options, such as timebase and stopAtCycle.

        If you have an extremely large graph, it is recommended that you use
        AHPGraph to do the graph partitioning instead of letting SST do it. You
        can do this by using the Device.set_partition() function and then
        setting nranks in this function to the total number of ranks used
        """
        if not os.path.exists('output'):
            os.makedirs('output')

        # If this is serial or sst is doing the partitioning,
        # just write the whole thing
        if nranks == 1:
            self = SSTGraph(self.flatten())
            self.verify_links()
            self.__write_model(f"output/{filename}", nranks, program_options)

        # Write a JSON file for each rank
        else:
            (base, ext) = os.path.splitext(f"output/{filename}")
            self.check_partition()

            for rank in range(nranks):
                rankGraph = self.flatten(rank=rank)
                # need to verify before we follow links because pruning
                # may make the overall graph invalid
                # (it will be valid for the current rank)
                rankGraph.verify_links()
                rankGraph = SSTGraph(rankGraph.follow_links(rank, True))
                rankGraph.__write_model(base + str(rank) + ext,
                                        nranks, program_options)

                # Manually remove each graph to make sure we don't overflow
                del rankGraph
                gc.collect()

    def __encode(self, attr, stringify: bool = False) -> dict:
        """
        Convert attributes into SST Params.

        SST only supports primitive types (bool, int, float) and lists of those
        types as parameters; everything else we will convert via JSON.
        If the attribute contains a __to_json__ method, then we will call it.
        Ignore bad conversions.
        """

        def supported_f(x) -> bool:
            """Return whether the type is supported by SST."""
            return isinstance(x, (bool, float, int, str))

        params = dict()
        for (key, val) in attr.items():
            native = supported_f(val)
            if not native and isinstance(val, list):
                native = all(map(supported_f, val))

            if native:
                params[key] = val if not stringify else str(val)
            elif hasattr(val, "__to_json__"):
                params[key] = val.__to_json__()
            else:
                try:
                    # serialize the value to json bytes,
                    # then deserialize into a python dict
                    params[key] = orjson.loads(
                        orjson.dumps(val, option=orjson.OPT_INDENT_2)
                    )
                except Exception:
                    pass

        return params

    def __build_model(self, self_partition: bool) -> dict:
        """Generate the model for the SST program."""
        # only import sst when we are going to build the graph inside of sst
        import sst
        n2c = dict()

        # Set up global parameters.
        global_params = self.__encode(self.attr)
        for (key, val) in global_params.items():
            sst.addGlobalParam(key, key, val)

        def recurseSubcomponents(dev: Device, comp: 'Component'):
            """Add subcomponents to the Device."""
            for (d1, n1, s1) in dev.subs:
                if d1.library is None:
                    raise RuntimeError(f"No SST library: {d1.name}")
                if s1 is None:
                    c1 = comp.setSubComponent(n1, d1.library)
                else:
                    c1 = comp.setSubComponent(n1, d1.library, s1)
                c1.addParams(self.__encode(
                    {'type': d1.type, 'model': d1.model} | d1.attr))
                n2c[d1.name] = c1
                for key in global_params:
                    c1.addGlobalParamSet(key)
                if len(d1.subs) > 0:
                    recurseSubcomponents(d1, c1)

        # First, we instantiate all of the components with
        # their attributes. Ignore Devices that have no library defined
        for d0 in self.devices.values():
            if d0.subOwner is None and d0.library is not None:
                c0 = sst.Component(d0.name, d0.library)
                c0.addParams(self.__encode(
                    {'type': d0.type, 'model': d0.model} | d0.attr))
                # Set the component partition if we are self-partitioning
                if self_partition:
                    thread = 0 if d0.partition[1] is None else d0.partition[1]
                    c0.setRank(d0.partition[0], thread)
                n2c[d0.name] = c0
                for key in global_params:
                    c0.addGlobalParamSet(key)
                recurseSubcomponents(d0, c0)

        # Second, link the component ports using graph links.
        for (p0, p1), t in self.links.items():
            if p0.device.library is not None and p1.device.library is not None:
                c0 = n2c[p0.device.name]
                c1 = n2c[p1.device.name]
                s0 = p0.get_name()
                s1 = p1.get_name()
                link = sst.Link(f'{p0}__{t}__{p1}')
                latency = t if t != '0s' else '1ps'
                link.connect((c0, s0, latency), (c1, s1, latency))

        # Return a map of component names to components.
        return n2c

    def __write_model(self, filename: str, nranks: int,
                      program_options: dict) -> None:
        """Write this DeviceGraph out as JSON."""
        model = dict()

        # Write the program options to the model.
        if program_options is None:
            model["program_options"] = dict()
        else:
            model["program_options"] = dict(program_options)

        # If running in parallel, then set up the SST SELF partitioner.
        if nranks > 1:
            model["program_options"]["partitioner"] = "sst.self"

        # Set up global parameters.
        global_params = self.__encode(self.attr, True)
        model["global_params"] = dict()
        for (key, val) in global_params.items():
            model["global_params"][key] = dict({key: val})
        global_set = list(global_params.keys())

        def recurseSubcomponents(dev: Device) -> list:
            """Add subcomponents to the Device."""
            subcomponents = list()
            for (d1, n1, s1) in dev.subs:
                if d1.library is None:
                    raise RuntimeError(f"No library: {d1.name}")

                item = {
                        "slot_name": n1,
                        "type": d1.library,
                        "slot_number": s1,
                        "params": self.__encode(
                            {'type': d1.type, 'model': d1.model} | d1.attr,
                            True),
                        "params_global_sets": global_set,
                    }
                if len(d1.subs) > 0:
                    item["subcomponents"] = recurseSubcomponents(d1)
                subcomponents.append(item)
            return subcomponents

        # Define all the components. We define the name, type, parameters,
        # and global parameters. Ignore Devices that have no library defined
        components = list()
        for d0 in self.devices.values():
            if d0.subOwner is None and d0.library is not None:
                component = {
                    "name": d0.name,
                    "type": d0.library,
                    "params": self.__encode(
                        {'type': d0.type, 'model': d0.model} | d0.attr,
                        True),
                    "params_global_sets": global_set,
                }
                if nranks > 1:
                    component["partition"] = {
                        "rank": d0.partition[0],
                        "thread": (0 if d0.partition[1] is None
                                   else d0.partition[1]),
                    }

                subcomponents = recurseSubcomponents(d0)
                if len(subcomponents) > 0:
                    component["subcomponents"] = subcomponents
                components.append(component)

        model["components"] = components

        # Now define the links between components.
        links = list()
        for (p0, p1), t in self.links.items():
            if p0.device.library is not None and p1.device.library is not None:
                latency = t if t != '0s' else '1ps'
                links.append(
                    {
                        "name": f'{p0}__{t}__{p1}',
                        "left": {
                            "component": p0.device.name,
                            "port": p0.get_name(),
                            "latency": latency,
                        },
                        "right": {
                            "component": p1.device.name,
                            "port": p1.get_name(),
                            "latency": latency,
                        },
                    }
                )

        model["links"] = links

        with open(filename, "wb") as jfile:
            jfile.write(orjson.dumps(model, option=orjson.OPT_INDENT_2))
